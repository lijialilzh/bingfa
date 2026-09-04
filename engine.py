#!/usr/bin/env python3
"""
并发测试引擎，支持两种模式：

  mode="full"       全部接口测试：每个账号独立浏览器上下文（模拟不同笔记本 + Chrome），
                    同时登录 -> 同时进入图像工作站阅片 -> 加载图像帧。
                    覆盖接口：登录、会话校验、图像元数据、序列数据、缩略图、图像帧、消息令牌。

  mode="frame_only" 过滤其他接口，只压测「图像帧」接口：
                    GET /xa_brain_encrypt/XA-00001/{study_uid}/{series_uid}/{sop_instance_uid}
                    （dcp 仅在准备阶段调用一次，用于获取帧列表，不计入测试。）

通过事件队列把每个用户的请求过程实时推送给 Web 服务。
"""

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright

# 默认配置（可在界面录入覆盖）
DEFAULT_CONFIG = {
    "base_url": "http://192.168.108.109:8000",
    "login_path": "/app/center/login",
    "viewer_path_template": "/app/txzq/view/{study_uid}",
    "password": "123qwe",
    "study_uid": "1.3.46.670589.28.68172260368172520200518000449629596",
    "series_uid": "1.3.46.670589.28.681722603681725.20200518010931497060.2.2",
    "product": "XA_BRAIN",
    "accounts": ["test"] + [f"test{i}" for i in range(2, 101)],
}

# 关键 API 路径 -> 类型标签
API_TAGS = [
    ("/api/v1/user/login", "登录"),
    ("/api/v1/user/checkLogin", "会话校验"),
    ("/api/v1/studies", "图像元数据"),
    ("/api/repacs/series", "序列数据"),
    ("/xa_brain_encrypt/", "图像帧"),
    ("/RESULT/thumbnail", "缩略图"),
    ("/api/v1/msg/token", "消息令牌"),
]

FRAME_PREFIX = "/xa_brain_encrypt/"


def tag_of(url: str) -> str:
    for prefix, label in API_TAGS:
        if prefix in url:
            return label
    return "其他"


def _is_hash(s: str) -> bool:
    """判断是否为 64 位十六进制（SHA256 哈希）。"""
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", s))


def to_password_hash(pwd: str) -> str:
    """把明文密码转成 SHA256 哈希；若已是哈希则原样返回。"""
    pwd = pwd.strip()
    if _is_hash(pwd):
        return pwd.lower()
    return hashlib.sha256(pwd.encode()).hexdigest()


def parse_accounts(text: str) -> list[str]:
    """解析账号录入文本，支持：
    - 每行一个账号，或「账号,密码」格式（密码可为明文或 SHA256 哈希）
    - 范围语法：test2-test100 或 test2~test100
    - 逗号/空格分隔
    返回账号列表（不含密码）。
    """
    return [c["account"] for c in parse_credentials(text)]


def parse_credentials(text: str) -> list[dict]:
    """解析账号文本，返回 [{"account": ..., "password": ...}]。
    每行格式：账号 或 账号,密码（密码可为明文或哈希）。
    """
    creds: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 每行可能含多个账号（空格分隔），但「账号,密码」含逗号需整体处理
        for part in line.split():
            part = part.strip()
            if not part:
                continue
            # 账号,密码 格式
            if "," in part:
                account, pwd = part.split(",", 1)
                account = account.strip()
                pwd = pwd.strip()
                if account:
                    creds.append({"account": account, "password": pwd})
                continue
            # 范围语法 prefixN-prefixM
            if "-" in part and part.count("-") == 1:
                a, b = part.split("-")
                m = _range_match(a, b)
                if m:
                    creds.extend({"account": x, "password": None} for x in m)
                    continue
            if "~" in part:
                a, b = part.split("~")
                m = _range_match(a, b)
                if m:
                    creds.extend({"account": x, "password": None} for x in m)
                    continue
            creds.append({"account": part, "password": None})
    # 去重保序（按账号）
    seen = set()
    out = []
    for c in creds:
        if c["account"] not in seen:
            seen.add(c["account"])
            out.append(c)
    return out


def _range_match(a: str, b: str) -> Optional[list[str]]:
    """若 a、b 形如 prefixN 且前缀相同，返回 [a..b] 列表。"""
    ma = re.fullmatch(r"(.+?)(\d+)", a)
    mb = re.fullmatch(r"(.+?)(\d+)", b)
    if not ma or not mb or ma.group(1) != mb.group(1):
        return None
    prefix = ma.group(1)
    start, end = int(ma.group(2)), int(mb.group(2))
    if start > end:
        start, end = end, start
    if end - start > 10000:
        return None
    return [f"{prefix}{i}" for i in range(start, end + 1)]


# ---------------- 随机化浏览器指纹 ----------------
# 模拟不同 Windows 电脑的 Chrome 浏览器特征

_WIN_VERSIONS = ["10.0", "10.0", "10.0", "11.0", "11.0"]  # Win10 居多
_CHROME_MAJOR = [120, 121, 122, 123, 124, 125, 126, 127, 128]
_RESOLUTIONS = [
    (1920, 1080), (1920, 1080), (1366, 768), (1536, 864),
    (2560, 1440), (1440, 900), (1600, 900), (1280, 720),
]
_TIMEZONES = ["Asia/Shanghai", "Asia/Shanghai", "Asia/Shanghai",
              "Asia/Hong_Kong", "Asia/Taipei", "Asia/Singapore",
              "Asia/Tokyo", "Asia/Seoul"]
_LANGS = ["zh-CN,zh;q=0.9", "zh-CN,zh;q=0.9", "zh-CN,zh;q=0.9",
          "zh-TW,zh;q=0.9", "en-US,en;q=0.9", "zh-HK,zh;q=0.9"]


def random_fingerprint() -> dict:
    """生成一套随机的 Windows Chrome 浏览器指纹。"""
    win = random.choice(_WIN_VERSIONS)
    major = random.choice(_CHROME_MAJOR)
    minor = random.randint(0, 99)
    build = random.randint(1000, 9999)
    ua = (f"Mozilla/5.0 (Windows NT {win}; Win64; x64) "
          f"AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{major}.0.{minor}.{build} Safari/537.36")
    w, h = random.choice(_RESOLUTIONS)
    return {
        "user_agent": ua,
        "viewport": {"width": w, "height": h},
        "timezone_id": random.choice(_TIMEZONES),
        "locale": random.choice(_LANGS),
    }


@dataclass
class UserState:
    user_id: int
    account: str
    status: str = "pending"          # pending/login/viewer/loading/done/error/timeout
    login_ms: float = 0.0
    viewer_ms: float = 0.0           # 阅片页 HTML 加载完成耗时
    first_frame_ms: float = 0.0      # 首帧出现耗时
    all_frames_ms: float = 0.0       # 整套图像全部加载完成耗时
    frames_loaded: int = 0
    total_frames: int = 0            # 该检查的总帧数
    # ---- 可审计证据 ----
    session_token: str = ""          # 登录后服务器下发的独立会话 token
    user_id_server: str = ""         # 服务器返回的 userId
    login_ts: float = 0.0            # 登录请求发出的绝对时间戳(epoch 秒)
    viewer_ts: float = 0.0           # 进入阅片的绝对时间戳
    first_frame_ts: float = 0.0      # 首帧请求的绝对时间戳
    error: str = ""


class TestEngine:
    """负责并发执行，事件通过 emit 回调推送。mode 决定测试范围。"""

    def __init__(self, users: int, observe_seconds: float,
                 emit: Callable[[dict], None], config: Optional[dict] = None,
                 mode: str = "full"):
        self.users = users
        self.observe_seconds = observe_seconds
        self.emit = emit
        self.mode = mode
        self.states: dict[int, UserState] = {}
        self._stop = False

        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.base_url = cfg["base_url"].rstrip("/")
        self.login_url = self.base_url + cfg["login_path"]
        self.viewer_url = self.base_url + cfg["viewer_path_template"].format(
            study_uid=cfg["study_uid"])
        self.default_password = cfg["password"]
        self.study_uid = cfg["study_uid"]
        self.series_uid = cfg.get("series_uid", "")
        # credentials: [{"account": str, "password": str|None}]
        self.credentials = parse_credentials(
            "\n".join(cfg.get("accounts", [])))
        self.frame_urls: list[str] = []

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        if self.mode == "frame_only":
            await self._run_frame_only()
        else:
            await self._run_full()

    # ---------------- 模式1：全部接口测试 ----------------

    async def _run_full(self) -> None:
        creds = self.credentials[: self.users]
        accounts = [c["account"] for c in creds]
        self.states = {i: UserState(user_id=i, account=accounts[i])
                       for i in range(self.users)}

        self.emit({"type": "start", "users": self.users,
                   "accounts": accounts, "mode": "full", "ts": time.time()})

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            # 分批创建独立上下文（每个用户随机指纹 = 模拟不同电脑）
            contexts = []
            for i, cred in enumerate(creds):
                fp = random_fingerprint()
                ctx = await browser.new_context(
                    viewport=fp["viewport"],
                    user_agent=fp["user_agent"],
                    timezone_id=fp["timezone_id"],
                    locale=fp["locale"],
                )
                contexts.append((i, cred, ctx))
                if (i + 1) % 20 == 0 or i == self.users - 1:
                    self.emit({"type": "progress",
                               "msg": f"已创建 {i + 1}/{self.users} 个独立上下文",
                               "ts": time.time()})

            login_barrier = asyncio.Barrier(self.users)
            viewer_barrier = asyncio.Barrier(self.users)

            tasks = [
                self._run_user_full(i, cred, ctx, login_barrier, viewer_barrier)
                for i, cred, ctx in contexts
            ]
            await asyncio.gather(*tasks)
            await browser.close()

        self.emit({"type": "summary", "states": self._snapshot(),
                   "ts": time.time()})

    async def _run_user_full(self, user_id: int, cred: dict, context,
                             login_barrier: asyncio.Barrier,
                             viewer_barrier: asyncio.Barrier) -> None:
        st = self.states[user_id]
        account = cred["account"]
        # 密码：优先用账号自带密码，否则用默认密码；统一转 SHA256 哈希
        raw_pwd = cred.get("password") or self.default_password
        pwd_hash = to_password_hash(raw_pwd)
        page = await context.new_page()

        # 监听请求/响应，实时推送
        async def on_request(req):
            if any(p in req.url for p, _ in API_TAGS):
                if FRAME_PREFIX in req.url:
                    st.frames_loaded += 1
                    if st.first_frame_ts == 0.0:
                        st.first_frame_ts = time.time()
                self.emit({
                    "type": "request",
                    "user_id": user_id,
                    "account": account,
                    "method": req.method,
                    "url": req.url,
                    "tag": tag_of(req.url),
                    "ts": time.time(),
                })

        async def on_response(res):
            if any(p in res.url for p, _ in API_TAGS):
                # 从图像元数据接口解析总帧数
                if "/api/v1/studies" in res.url and res.status == 200:
                    try:
                        body = await res.text()
                        data = json.loads(body)
                        total = 0
                        for study in data.get("data", []):
                            for series in study.get("series", []):
                                total += int(series.get("imgFrameNumber", 0))
                        if total > 0:
                            st.total_frames = total
                            self.emit({
                                "type": "total_frames",
                                "user_id": user_id,
                                "account": account,
                                "total_frames": total,
                                "ts": time.time(),
                            })
                    except Exception:
                        pass
                self.emit({
                    "type": "response",
                    "user_id": user_id,
                    "account": account,
                    "status": res.status,
                    "url": res.url,
                    "tag": tag_of(res.url),
                    "ts": time.time(),
                })

        page.on("request", lambda req: asyncio.create_task(on_request(req)))
        page.on("response", lambda res: asyncio.create_task(on_response(res)))

        try:
            # ---- 阶段1：登录（API 直接登录，拿到 cookie 注入浏览器） ----
            await login_barrier.wait()
            st.status = "login"
            self.emit({"type": "user_status", "user_id": user_id,
                       "account": account, "status": "login", "ts": time.time()})

            t0 = time.perf_counter()
            st.login_ts = time.time()
            login_ok, token, server_uid = await self._api_login(
                context, account, pwd_hash)
            st.login_ms = (time.perf_counter() - t0) * 1000
            st.session_token = token
            st.user_id_server = server_uid

            if not login_ok:
                st.status = "error"
                st.error = "登录失败"
                self.emit({"type": "user_status", "user_id": user_id,
                           "account": account, "status": "error",
                           "error": st.error, "ts": time.time()})
                return

            self.emit({"type": "login_ok", "user_id": user_id,
                       "account": account, "login_ms": st.login_ms,
                       "session_token": token,
                       "ts": time.time()})

            # ---- 阶段2：同时进入阅片（同一个检查，各自独立打开） ----
            await viewer_barrier.wait()
            st.status = "viewer"
            self.emit({"type": "user_status", "user_id": user_id,
                       "account": account, "status": "viewer", "ts": time.time()})

            t1 = time.perf_counter()
            st.viewer_ts = time.time()
            # 并发下服务器响应慢，页面加载超时放宽到 120 秒
            await page.goto(self.viewer_url, wait_until="domcontentloaded", timeout=120000)
            st.viewer_ms = (time.perf_counter() - t1) * 1000
            st.status = "loading"
            self.emit({"type": "viewer_ok", "user_id": user_id,
                       "account": account, "viewer_ms": st.viewer_ms,
                       "ts": time.time()})

            # 等待整套图像全部加载完成（与手动录制口径一致）
            # 上限：observe_seconds 秒，避免个别用户卡死拖垮整体
            deadline = t1 + self.observe_seconds
            while time.perf_counter() < deadline:
                if st.frames_loaded > 0 and st.first_frame_ms == 0.0:
                    st.first_frame_ms = (time.perf_counter() - t1) * 1000
                # 全部帧加载完成
                if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
                    st.all_frames_ms = (time.perf_counter() - t1) * 1000
                    break
                await asyncio.sleep(0.2)

            # 区分：真正加载完 vs 超时未加载完
            if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
                st.status = "done"          # 真正加载完
            else:
                st.status = "timeout"       # 超时未加载完
            self.emit({"type": "user_done", "user_id": user_id,
                       "account": account, "frames_loaded": st.frames_loaded,
                       "total_frames": st.total_frames,
                       "all_frames_ms": st.all_frames_ms,
                       "status": st.status,
                       "ts": time.time()})
        except Exception as e:
            st.status = "error"
            st.error = f"{type(e).__name__}: {e}"
            self.emit({"type": "user_status", "user_id": user_id,
                       "account": account, "status": "error",
                       "error": st.error, "ts": time.time()})
        finally:
            await context.close()

    async def _api_login(self, context, account: str, pwd_hash: str):
        """通过 API 登录，把返回的 cookie 注入浏览器上下文。
        返回 (是否成功, session_token, server_userId)。"""
        login_api = self.base_url + "/api/v1/user/login"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    login_api,
                    json={"name": account, "password": pwd_hash},
                )
                data = resp.json()
                if data.get("code") != 10000:
                    return False, "", ""
                # 登录成功后，服务器通过 Set-Cookie 下发会话
                cookies = []
                for name, value in resp.cookies.items():
                    cookies.append({
                        "name": name,
                        "value": value,
                        "domain": "192.168.108.109",
                        "path": "/",
                    })
                # 兜底：手动构造前端依赖的 cookie
                uid = data.get("data", {}).get("userId")
                if uid is not None:
                    cookies.append({"name": "userId", "value": str(uid),
                                    "domain": "192.168.108.109", "path": "/"})
                    cookies.append({"name": "name", "value": account,
                                    "domain": "192.168.108.109", "path": "/"})
                if cookies:
                    await context.add_cookies(cookies)
                token = resp.cookies.get("token", "")
                return True, token, str(uid or "")
        except Exception:
            return False, "", ""

    # ---------------- 模式2：只压测图像帧接口 ----------------

    async def _fetch_frame_urls(self) -> list[str]:
        """准备阶段：从 dcp 接口获取全部帧 URL（仅调用一次，不计入测试）。"""
        url = f"{self.base_url}/api/repacs/series/{self.series_uid}/dcp"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            data = resp.json()
            images = data.get("images", [])
            return [f"{self.base_url}/{img['storagePath']}" for img in images]

    async def _run_frame_only(self) -> None:
        self.emit({"type": "progress",
                   "msg": "准备：获取图像帧列表 (dcp) ...", "ts": time.time()})
        try:
            self.frame_urls = await self._fetch_frame_urls()
        except Exception as e:
            self.emit({"type": "error",
                       "msg": f"获取帧列表失败: {type(e).__name__}: {e}",
                       "ts": time.time()})
            return
        total = len(self.frame_urls)
        if total == 0:
            self.emit({"type": "error", "msg": "未获取到图像帧",
                       "ts": time.time()})
            return

        accounts = [f"user{i + 1}" for i in range(self.users)]
        self.states = {
            i: UserState(user_id=i, account=accounts[i], total_frames=total)
            for i in range(self.users)
        }
        self.emit({"type": "start", "users": self.users, "accounts": accounts,
                   "total_frames": total, "mode": "frame_only",
                   "ts": time.time()})

        tasks = [self._run_user_frame_only(i) for i in range(self.users)]
        await asyncio.gather(*tasks)

        self.emit({"type": "summary", "states": self._snapshot(),
                   "ts": time.time()})

    async def _run_user_frame_only(self, user_id: int) -> None:
        st = self.states[user_id]
        st.status = "loading"
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": st.account, "status": "loading", "ts": time.time()})

        t0 = time.perf_counter()
        st.viewer_ts = time.time()
        deadline = t0 + self.observe_seconds

        async with httpx.AsyncClient(timeout=120) as client:
            for url in self.frame_urls:
                if self._stop or time.perf_counter() > deadline:
                    break
                t_req = time.perf_counter()
                try:
                    resp = await client.get(url)
                    ok = resp.status_code == 200
                    if ok:
                        st.frames_loaded += 1
                        if st.first_frame_ts == 0.0:
                            st.first_frame_ts = time.time()
                            st.first_frame_ms = (t_req - t0) * 1000
                    self.emit({"type": "request", "user_id": user_id,
                               "account": st.account, "method": "GET",
                               "url": url, "tag": "图像帧", "ts": time.time()})
                    self.emit({"type": "response", "user_id": user_id,
                               "account": st.account, "status": resp.status_code,
                               "url": url, "tag": "图像帧", "ts": time.time()})
                except Exception as e:
                    self.emit({"type": "response", "user_id": user_id,
                               "account": st.account, "status": 0,
                               "url": url, "tag": "图像帧",
                               "error": f"{type(e).__name__}: {e}",
                               "ts": time.time()})
                if st.frames_loaded >= st.total_frames:
                    break

        st.all_frames_ms = (time.perf_counter() - t0) * 1000
        if st.frames_loaded >= st.total_frames:
            st.status = "done"
        else:
            st.status = "timeout"
        self.emit({"type": "user_done", "user_id": user_id,
                   "account": st.account, "frames_loaded": st.frames_loaded,
                   "total_frames": st.total_frames,
                   "all_frames_ms": st.all_frames_ms,
                   "status": st.status, "ts": time.time()})

    # ---------------- 公共 ----------------

    def _snapshot(self) -> list[dict]:
        return [
            {
                "user_id": s.user_id,
                "account": s.account,
                "status": s.status,
                "login_ms": round(s.login_ms, 1),
                "viewer_ms": round(s.viewer_ms, 1),
                "first_frame_ms": round(s.first_frame_ms, 1),
                "all_frames_ms": round(s.all_frames_ms, 1),
                "frames_loaded": s.frames_loaded,
                "total_frames": s.total_frames,
                "session_token": s.session_token,
                "user_id_server": s.user_id_server,
                "login_ts": round(s.login_ts, 3),
                "viewer_ts": round(s.viewer_ts, 3),
                "first_frame_ts": round(s.first_frame_ts, 3),
                "error": s.error,
            }
            for s in self.states.values()
        ]


