#!/usr/bin/env python3
"""
并发测试引擎：纯 HTTP 接口并发压测。

每个用户 = 一个并发任务，循环依次调用以下接口（模拟真实用户访问）：
  1. 登录            POST {login_api_path}
  2. 会话校验        GET  {check_login_prefix}
  3. 消息令牌        GET  {msg_token_prefix}
  4. 图像元数据      GET  {studies_api_path}
  5. 序列数据        GET  {dcp_api_path_template}
  6. 缩略图          GET  {thumbnail_prefix}/{series_uid}/thumbnail.jpg
  7. 图像帧          GET  {frame_prefix}...（从序列数据接口解析出帧 URL）

通过事件队列把每个用户的请求过程实时推送给 Web 服务。
"""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

# 默认配置（可在界面录入覆盖）
DEFAULT_CONFIG = {
    "base_url": "http://192.168.108.109:8000",
    "password": "123qwe",
    "study_uid": "1.3.46.670589.28.68172260368172520200518000449629596",
    "series_uid": "1.3.46.670589.28.681722603681725.20200518010931497060.2.2",
    "product": "XA_BRAIN",
    "accounts": ["test"] + [f"test{i}" for i in range(2, 101)],
    # ---- 接口路径（换产品时按需修改）----
    "login_api_path": "/api/v1/user/login",
    "check_login_prefix": "/api/v1/user/checkLogin",
    "checklist_api_path": "/api/v1/studies/query/online",
    "studies_api_path": "/api/v1/studies",
    "dcp_api_path_template": "/api/repacs/series/{series_uid}/dcp",
    "frame_prefix": "/xa_brain_encrypt/",
    "thumbnail_prefix": "/RESULT/thumbnail",
    "msg_token_prefix": "/api/v1/msg/token",
    # 每个用户内部并发下载图像帧的连接数（模拟浏览器同域名并发连接）
    "frame_concurrency": 6,
}

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
    round_times: list = None         # 每轮整套流程耗时（ms）
    round_login_ms: list = None      # 每轮登录耗时
    round_viewer_ms: list = None     # 每轮阅片页加载耗时
    round_first_frame_ms: list = None  # 每轮首张耗时
    round_frame_ms: list = None     # 每轮图像下载耗时
    # ---- 可审计证据 ----
    session_token: str = ""          # 登录后服务器下发的独立会话 token
    user_id_server: str = ""         # 服务器返回的 userId
    login_ts: float = 0.0            # 登录请求发出的绝对时间戳(epoch 秒)
    viewer_ts: float = 0.0           # 进入阅片的绝对时间戳
    first_frame_ts: float = 0.0      # 首帧请求的绝对时间戳
    error: str = ""

    def __post_init__(self):
        if self.round_times is None:
            self.round_times = []
        if self.round_login_ms is None:
            self.round_login_ms = []
        if self.round_viewer_ms is None:
            self.round_viewer_ms = []
        if self.round_first_frame_ms is None:
            self.round_first_frame_ms = []
        if self.round_frame_ms is None:
            self.round_frame_ms = []


class TestEngine:
    """纯 HTTP 接口并发压测，事件通过 emit 回调推送。"""

    def __init__(self, users: int, observe_seconds: float,
                 emit: Callable[[dict], None], config: Optional[dict] = None,
                 mode: str = "full", rounds: int = 1):
        self.users = users
        self.observe_seconds = observe_seconds
        self.emit = emit
        self.mode = mode
        self.rounds = rounds
        self.states: dict[int, UserState] = {}
        self._stop = False

        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.base_url = cfg["base_url"].rstrip("/")
        self.default_password = cfg["password"]
        self.study_uid = cfg["study_uid"]
        self.series_uid = cfg.get("series_uid", "")
        # ---- 接口路径（可配置，换产品时修改）----
        self.login_api_path = cfg.get("login_api_path", "/api/v1/user/login")
        self.check_login_prefix = cfg.get("check_login_prefix", "/api/v1/user/checkLogin")
        self.checklist_api_path = cfg.get("checklist_api_path", "/api/v1/studies/query/online")
        self.studies_api_path = cfg.get("studies_api_path", "/api/v1/studies")
        self.dcp_api_path_template = cfg.get(
            "dcp_api_path_template", "/api/repacs/series/{series_uid}/dcp")
        self.frame_prefix = cfg.get("frame_prefix", "/xa_brain_encrypt/")
        self.thumbnail_prefix = cfg.get("thumbnail_prefix", "/RESULT/thumbnail")
        self.msg_token_prefix = cfg.get("msg_token_prefix", "/api/v1/msg/token")
        # 每个用户内部并发下载图像帧的连接数（模拟浏览器同域名并发连接）
        self.frame_concurrency = max(1, int(cfg.get("frame_concurrency", 6) or 6))
        # 自定义接口列表（录制导入），存在则并发测试按此列表循环调用
        self.custom_apis = cfg.get("apis") or []
        # credentials: [{"account": str, "password": str|None}]
        self.credentials = parse_credentials(
            "\n".join(cfg.get("accounts", [])))

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        if self.mode == "frame_only":
            await self._run_frame_only()
        elif self.custom_apis:
            await self._run_custom()
        else:
            await self._run_full()

    async def _run_custom(self) -> None:
        """按录制导入的接口列表循环压测。
        每个用户把接口列表循环执行 rounds 轮（默认 1 轮），执行完即停。"""
        creds = self.credentials[: self.users]
        accounts = [c["account"] for c in creds]
        self.states = {i: UserState(user_id=i, account=accounts[i])
                       for i in range(self.users)}

        self.emit({"type": "start", "users": self.users,
                   "accounts": accounts, "mode": "custom", "ts": time.time()})

        tasks = [self._run_user_custom(i, cred) for i, cred in enumerate(creds)]
        await asyncio.gather(*tasks)

        self.emit({"type": "summary", "states": self._snapshot(),
                   "ts": time.time()})

    async def _run_user_custom(self, user_id: int, cred: dict) -> None:
        st = self.states[user_id]
        account = cred["account"]
        raw_pwd = cred.get("password") or self.default_password
        pwd_hash = to_password_hash(raw_pwd)

        st.status = "loading"
        st.viewer_ts = time.time()
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": account, "status": "loading", "ts": time.time()})

        t0 = time.perf_counter()
        # 自定义接口模式：按轮数执行，每轮把接口列表从头到尾执行一遍
        rounds = max(1, int(getattr(self, "rounds", 1) or 1))
        total_requests = len(self.custom_apis) * rounds

        def fill(text: str) -> str:
            return (text
                    .replace("{account}", account)
                    .replace("{password_hash}", pwd_hash)
                    .replace("{study_uid}", self.study_uid)
                    .replace("{series_uid}", self.series_uid))

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for round_idx in range(rounds):
                if self._stop:
                    break
                # 广播轮次开始
                self.emit({"type": "round_start", "user_id": user_id,
                           "account": account, "round": round_idx + 1,
                           "total_rounds": rounds, "ts": time.time()})
                for api in self.custom_apis:
                    if self._stop:
                        break
                    name = api.get("name") or api.get("url", "接口")
                    method = (api.get("method") or "GET").upper()
                    url = fill(api.get("url", ""))
                    body = fill(api.get("body", ""))
                    content_type = api.get("content_type") or "application/json"
                    # 文件上传接口：multipart/form-data
                    upload_file = api.get("upload_file") or ""
                    upload_field = api.get("upload_field") or "file"
                    if not url:
                        continue
                    if url.startswith("/"):
                        url = self.base_url + url
                    t_req = time.perf_counter()
                    await self._emit_req(user_id, account, method, url, name)
                    try:
                        if method == "POST":
                            if upload_file:
                                # multipart 文件上传
                                file_path = Path(__file__).parent / "uploads" / upload_file
                                if not file_path.exists():
                                    raise RuntimeError(f"上传文件不存在：{upload_file}")
                                with open(file_path, "rb") as f:
                                    files = {upload_field: (upload_file, f, "application/octet-stream")}
                                    # 额外字段（如 name=null）
                                    extra_data = {}
                                    if body:
                                        try:
                                            extra_data = json.loads(body)
                                        except Exception:
                                            extra_data = {}
                                    resp = await client.post(url, files=files, data=extra_data)
                            elif content_type == "application/x-www-form-urlencoded":
                                resp = await client.post(url, data=body,
                                                         headers={"Content-Type": content_type})
                            else:
                                resp = await client.post(url, content=body,
                                                         headers={"Content-Type": content_type})
                        else:
                            resp = await client.get(url)
                        elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                        st.frames_loaded += 1  # 请求计数
                        await self._emit_resp(user_id, account, resp.status_code,
                                              url, name, elapsed,
                                              size=len(resp.content))
                    except Exception as e:
                        elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                        st.frames_loaded += 1  # 失败也计数
                        await self._emit_resp(user_id, account, 0, url, name,
                                              elapsed, f"{type(e).__name__}: {e}")

        st.all_frames_ms = (time.perf_counter() - t0) * 1000
        st.status = "stopped" if self._stop else "done"
        self.emit({"type": "user_done", "user_id": user_id,
                   "account": account, "frames_loaded": st.frames_loaded,
                   "total_frames": st.total_frames,
                   "all_frames_ms": st.all_frames_ms,
                   "status": st.status, "ts": time.time()})

    async def _run_full(self) -> None:
        creds = self.credentials[: self.users]
        accounts = [c["account"] for c in creds]
        self.states = {i: UserState(user_id=i, account=accounts[i])
                       for i in range(self.users)}

        self.emit({"type": "start", "users": self.users,
                   "accounts": accounts, "mode": "full", "ts": time.time()})

        tasks = [self._run_user(i, cred) for i, cred in enumerate(creds)]
        await asyncio.gather(*tasks)

        self.emit({"type": "summary", "states": self._snapshot(),
                   "ts": time.time()})

    async def _run_frame_only(self) -> None:
        """只压测图像帧接口：先通过 study_uid 解析序列，再获取帧列表，循环下载。"""
        self.emit({"type": "progress",
                   "msg": "准备：获取图像元数据 (studies) ...", "ts": time.time()})
        # 1. 通过 study_uid 获取序列列表，自动解析 series_uid（换图后无需手动改 series_uid）
        series_uids: list[str] = []
        studies_url = (self.base_url + self.studies_api_path +
                       f"?studyInstanceUID={self.study_uid}&taskType=xa_brain&product=XA_BRAIN")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(studies_url)
                data = resp.json()
                for study in data.get("data", []):
                    for series in study.get("series", []):
                        suid = series.get("seriesInstanceUID")
                        if suid:
                            series_uids.append(suid)
        except Exception as e:
            self.emit({"type": "error",
                       "msg": f"获取图像元数据失败: {type(e).__name__}: {e}",
                       "ts": time.time()})
            return
        if not series_uids:
            # studies 接口可能返回空（如 CT 产品），回退用配置的 series_uid
            if self.series_uid:
                series_uids = [self.series_uid]
                self.emit({"type": "progress",
                           "msg": "studies 未返回序列，改用配置的 series_uid",
                           "ts": time.time()})
            else:
                self.emit({"type": "error",
                           "msg": "未获取到序列，请检查 study_uid 或 series_uid 是否正确",
                           "ts": time.time()})
                return
        # 优先用配置的 series_uid（若在列表中），否则用第一个序列
        series_uid = self.series_uid if self.series_uid in series_uids else series_uids[0]

        self.emit({"type": "progress",
                   "msg": f"准备：获取图像帧列表 (dcp, 共 {len(series_uids)} 个序列) ...",
                   "ts": time.time()})
        dcp_url = self.base_url + self.dcp_api_path_template.format(
            series_uid=series_uid)
        frame_urls = []
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(dcp_url)
                images = resp.json().get("images", [])
                frame_urls = [f"{self.base_url}/{img['storagePath']}"
                              for img in images]
        except Exception as e:
            self.emit({"type": "error",
                       "msg": f"获取帧列表失败: {type(e).__name__}: {e}",
                       "ts": time.time()})
            return
        total = len(frame_urls)
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

        tasks = [self._run_user_frame_only(i, frame_urls)
                 for i in range(self.users)]
        await asyncio.gather(*tasks)

        self.emit({"type": "summary", "states": self._snapshot(),
                   "ts": time.time()})

    async def _run_user_frame_only(self, user_id: int,
                                   frame_urls: list[str]) -> None:
        st = self.states[user_id]
        st.status = "loading"
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": st.account, "status": "loading", "ts": time.time()})

        t0 = time.perf_counter()
        st.viewer_ts = time.time()

        async with httpx.AsyncClient(timeout=120) as client:
            # 一套帧的数量：优先用 total_frames，否则用帧列表长度
            set_size = st.total_frames if st.total_frames > 0 else len(frame_urls)
            for round_idx in range(self.rounds):
                if self._stop:
                    break
                # 每轮重置帧计数，进度从 0 开始
                st.frames_loaded = 0
                self.emit({"type": "round_start", "user_id": user_id,
                           "account": st.account, "round": round_idx + 1,
                           "total_rounds": self.rounds, "ts": time.time()})
                round_ms = await self._download_frames(user_id, st.account,
                                                       frame_urls, set_size,
                                                       client, st, t0,
                                                       round_idx=round_idx + 1)
                st.round_times.append(round_ms)
                self.emit({"type": "round_done", "user_id": user_id,
                           "account": st.account, "round": round_idx + 1,
                           "total_rounds": self.rounds,
                           "round_ms": round_ms,
                           "frames_loaded": st.frames_loaded,
                           "ts": time.time()})

        st.all_frames_ms = (time.perf_counter() - t0) * 1000
        if self._stop:
            st.status = "stopped"
        elif st.total_frames > 0 and st.frames_loaded >= st.total_frames:
            st.status = "done"
        else:
            st.status = "timeout"
        self.emit({"type": "user_done", "user_id": user_id,
                   "account": st.account, "frames_loaded": st.frames_loaded,
                   "total_frames": st.total_frames,
                   "all_frames_ms": st.all_frames_ms,
                   "round_times": st.round_times,
                   "status": st.status, "ts": time.time()})

    async def _emit_req(self, user_id: int, account: str, method: str,
                        url: str, tag: str) -> None:
        self.emit({"type": "request", "user_id": user_id, "account": account,
                   "method": method, "url": url, "tag": tag, "ts": time.time()})

    async def _emit_resp(self, user_id: int, account: str, status: int,
                         url: str, tag: str, elapsed_ms: float,
                         error: str = "", size: int = 0,
                         round_idx: int = 0) -> None:
        ev = {"type": "response", "user_id": user_id, "account": account,
              "status": status, "url": url, "tag": tag,
              "elapsed_ms": elapsed_ms, "size": size, "ts": time.time()}
        # 优先用传入的 round_idx，否则用当前轮次
        r = round_idx or getattr(self, "_current_round", 0)
        if r:
            ev["round"] = r
        if error:
            ev["error"] = error
        self.emit(ev)

    async def _download_frames(self, user_id: int, account: str,
                               frame_urls: list[str], set_size: int,
                               client: httpx.AsyncClient, st: UserState,
                               t0: float, round_idx: int = 0) -> float:
        """并发下载一套图像帧（模拟浏览器同域名并发连接）。

        返回本轮下载耗时（ms）。
        """
        sem = asyncio.Semaphore(self.frame_concurrency)
        urls = [frame_urls[i % len(frame_urls)] for i in range(set_size)]
        round_t0 = time.perf_counter()

        async def fetch_one(url: str) -> None:
            async with sem:
                if self._stop:
                    return
                t_req = time.perf_counter()
                await self._emit_req(user_id, account, "GET", url, "图像帧")
                try:
                    resp = await client.get(url)
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    if resp.status_code == 200:
                        st.frames_loaded += 1
                        if st.first_frame_ts == 0.0:
                            st.first_frame_ts = time.time()
                            st.first_frame_ms = (t_req - t0) * 1000
                    await self._emit_resp(user_id, account, resp.status_code,
                                          url, "图像帧", elapsed,
                                          size=len(resp.content),
                                          round_idx=round_idx)
                except Exception as e:
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    await self._emit_resp(user_id, account, 0, url, "图像帧",
                                          elapsed, f"{type(e).__name__}: {e}",
                                          round_idx=round_idx)

        await asyncio.gather(*(fetch_one(u) for u in urls))
        return round((time.perf_counter() - round_t0) * 1000, 1)

    async def _run_user(self, user_id: int, cred: dict) -> None:
        st = self.states[user_id]
        account = cred["account"]
        raw_pwd = cred.get("password") or self.default_password
        pwd_hash = to_password_hash(raw_pwd)

        st.status = "login"
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": account, "status": "login", "ts": time.time()})

        t0 = time.perf_counter()

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for round_idx in range(self.rounds):
                if self._stop:
                    break
                self._current_round = round_idx + 1
                if self.rounds > 1:
                    self.emit({"type": "round_start", "user_id": user_id,
                               "account": account, "round": round_idx + 1,
                               "total_rounds": self.rounds, "ts": time.time()})
                round_t0 = time.perf_counter()
                ok, r_login, r_viewer, r_first, r_frames = await self._run_user_one_round(
                    user_id, account, pwd_hash, st, client, t0, round_idx)
                round_ms = round((time.perf_counter() - round_t0) * 1000, 1)
                st.round_times.append(round_ms)
                st.round_login_ms.append(r_login)
                st.round_viewer_ms.append(r_viewer)
                st.round_first_frame_ms.append(r_first)
                st.round_frame_ms.append(r_frames)
                if self.rounds > 1:
                    self.emit({"type": "round_done", "user_id": user_id,
                               "account": account, "round": round_idx + 1,
                               "total_rounds": self.rounds,
                               "round_ms": round_ms,
                               "frames_loaded": st.frames_loaded,
                               "ts": time.time()})
                if not ok and not self._stop:
                    break  # 登录失败等致命错误，不再继续后续轮次

        # 3 轮总耗时 = 各轮耗时之和（而非 t0 到结束，避免轮间间隔被计入）
        st.all_frames_ms = round(sum(st.round_times), 1) if st.round_times else (time.perf_counter() - t0) * 1000
        if self._stop:
            st.status = "stopped"
        elif st.total_frames > 0 and st.frames_loaded >= st.total_frames:
            st.status = "done"
        else:
            st.status = "timeout"
        self.emit({"type": "user_done", "user_id": user_id,
                   "account": account, "frames_loaded": st.frames_loaded,
                   "total_frames": st.total_frames,
                   "all_frames_ms": st.all_frames_ms,
                   "round_times": st.round_times,
                   "status": st.status, "ts": time.time()})

    async def _run_user_one_round(self, user_id: int, account: str,
                                  pwd_hash: str, st: UserState,
                                  client: httpx.AsyncClient,
                                  t0: float, round_idx: int) -> tuple:
        """执行一轮完整流程：登录→检查列表→图像元数据→序列数据→缩略图→图像帧。
        返回 (ok, login_ms, viewer_ms, first_frame_ms)。"""
        r_login = 0.0
        r_viewer = 0.0
        r_first = 0.0
        t_login_ok = 0.0
        # ---- 1. 登录 ----
        login_url = self.base_url + self.login_api_path
        login_body = json.dumps({"name": account, "password": pwd_hash})
        t_req = time.perf_counter()
        st.login_ts = time.time()
        await self._emit_req(user_id, account, "POST", login_url, "登录")
        try:
            resp = await client.post(login_url, content=login_body,
                                     headers={"Content-Type": "application/json"})
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            st.login_ms = elapsed
            r_login = elapsed
            data = resp.json()
            if data.get("code") == 10000:
                st.session_token = resp.cookies.get("token", "")
                st.user_id_server = str(data.get("data", {}).get("userId", ""))
                st.viewer_ts = time.time()
                t_login_ok = time.perf_counter()
                await self._emit_resp(user_id, account, resp.status_code,
                                      login_url, "登录", elapsed,
                                      size=len(resp.content))
                self.emit({"type": "login_ok", "user_id": user_id,
                           "account": account, "login_ms": elapsed,
                           "session_token": st.session_token,
                           "ts": time.time()})
                st.status = "viewer"
                self.emit({"type": "user_status", "user_id": user_id,
                           "account": account, "status": "viewer",
                           "ts": time.time()})
            else:
                await self._emit_resp(user_id, account, resp.status_code,
                                      login_url, "登录", elapsed,
                                      size=len(resp.content))
                st.status = "error"
                st.error = "登录失败"
                self.emit({"type": "user_status", "user_id": user_id,
                           "account": account, "status": "error",
                           "error": st.error, "ts": time.time()})
                return (False, r_login, r_viewer, r_first, 0)
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, login_url, "登录",
                                  elapsed, f"{type(e).__name__}: {e}")
            st.status = "error"
            st.error = f"{type(e).__name__}: {e}"
            self.emit({"type": "user_status", "user_id": user_id,
                       "account": account, "status": "error",
                       "error": st.error, "ts": time.time()})
            return (False, r_login, r_viewer, r_first, 0)

        # ---- 2. 会话校验 ----
        check_url = self.base_url + self.check_login_prefix
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "GET", check_url, "会话校验")
        try:
            resp = await client.get(check_url)
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, resp.status_code,
                                  check_url, "会话校验", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, check_url, "会话校验",
                                  elapsed, f"{type(e).__name__}: {e}")

        # ---- 2.5 消息令牌 ----
        msg_token_url = self.base_url + self.msg_token_prefix
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "GET", msg_token_url, "消息令牌")
        try:
            resp = await client.get(msg_token_url)
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, resp.status_code,
                                  msg_token_url, "消息令牌", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, msg_token_url,
                                  "消息令牌", elapsed, f"{type(e).__name__}: {e}")

        # ---- 2.8 检查列表 ----
        checklist_url = self.base_url + self.checklist_api_path
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "POST", checklist_url, "检查列表")
        try:
            resp = await client.post(
                checklist_url,
                json={"order_by": [{"studyDate": "desc"}],
                      "page": {"no": 1, "length": 100}})
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, resp.status_code,
                                  checklist_url, "检查列表", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, checklist_url,
                                  "检查列表", elapsed, f"{type(e).__name__}: {e}")

        # ---- 3. 图像元数据 ----
        studies_url = (self.base_url + self.studies_api_path +
                       f"?studyInstanceUID={self.study_uid}&taskType=xa_brain&product=XA_BRAIN")
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "GET", studies_url, "图像元数据")
        series_uids: list[str] = []
        try:
            resp = await client.get(studies_url)
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            try:
                data = resp.json()
                total = 0
                for study in data.get("data", []):
                    for series in study.get("series", []):
                        total += int(series.get("imgFrameNumber", 0))
                        suid = series.get("seriesInstanceUID")
                        if suid:
                            series_uids.append(suid)
                if total > 0:
                    st.total_frames = total
                    self.emit({"type": "total_frames", "user_id": user_id,
                               "account": account, "total_frames": total,
                               "ts": time.time()})
            except Exception:
                pass
            await self._emit_resp(user_id, account, resp.status_code,
                                  studies_url, "图像元数据", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, studies_url,
                                  "图像元数据", elapsed, f"{type(e).__name__}: {e}")

        # ---- 4. 序列数据 ----
        series_uid = (self.series_uid if self.series_uid in series_uids
                      else (series_uids[0] if series_uids else self.series_uid))
        dcp_url = self.base_url + self.dcp_api_path_template.format(
            series_uid=series_uid)
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "GET", dcp_url, "序列数据")
        frame_urls = []
        try:
            resp = await client.get(dcp_url)
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            try:
                images = resp.json().get("images", [])
                frame_urls = [f"{self.base_url}/{img['storagePath']}"
                              for img in images]
                if st.total_frames <= 0 and frame_urls:
                    st.total_frames = len(frame_urls)
                    self.emit({"type": "total_frames", "user_id": user_id,
                               "account": account,
                               "total_frames": st.total_frames,
                               "ts": time.time()})
            except Exception:
                pass
            await self._emit_resp(user_id, account, resp.status_code,
                                  dcp_url, "序列数据", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, dcp_url, "序列数据",
                                  elapsed, f"{type(e).__name__}: {e}")

        # ---- 5. 缩略图 ----
        thumb_url = f"{self.base_url}{self.thumbnail_prefix}/{series_uid}/thumbnail.jpg"
        t_req = time.perf_counter()
        await self._emit_req(user_id, account, "GET", thumb_url, "缩略图")
        try:
            resp = await client.get(thumb_url)
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, resp.status_code,
                                  thumb_url, "缩略图", elapsed,
                                  size=len(resp.content))
        except Exception as e:
            elapsed = round((time.perf_counter() - t_req) * 1000, 1)
            await self._emit_resp(user_id, account, 0, thumb_url, "缩略图",
                                  elapsed, f"{type(e).__name__}: {e}")

        # ---- 6. 图像帧 ----
        # viewer_ms = 登录成功到开始下载图像帧的耗时（会话校验+消息令牌+检查列表+元数据+序列数据+缩略图）
        r_viewer = round((time.perf_counter() - t_login_ok) * 1000, 1) if t_login_ok > 0 else 0
        st.viewer_ms = r_viewer
        st.status = "loading"
        self.emit({"type": "viewer_ok", "user_id": user_id,
                   "account": account, "viewer_ms": r_viewer,
                   "ts": time.time()})
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": account, "status": "loading", "ts": time.time()})
        if not frame_urls:
            st.status = "timeout"
            return (True, r_login, r_viewer, r_first, 0)

        set_size = st.total_frames if st.total_frames > 0 else len(frame_urls)
        st.frames_loaded = 0
        st.first_frame_ts = 0.0
        st.first_frame_ms = 0.0
        round_t0_frames = time.perf_counter()
        await self._download_frames(user_id, account, frame_urls, set_size,
                                    client, st, round_t0_frames, round_idx=round_idx + 1)
        r_first = st.first_frame_ms
        r_frames = round((time.perf_counter() - round_t0_frames) * 1000, 1)
        return (True, r_login, r_viewer, r_first, r_frames)

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
                "round_times": list(s.round_times),
                "round_login_ms": list(s.round_login_ms),
                "round_viewer_ms": list(s.round_viewer_ms),
                "round_first_frame_ms": list(s.round_first_frame_ms),
                "round_frame_ms": list(s.round_frame_ms),
                "session_token": s.session_token,
                "user_id_server": s.user_id_server,
                "login_ts": round(s.login_ts, 3),
                "viewer_ts": round(s.viewer_ts, 3),
                "first_frame_ts": round(s.first_frame_ts, 3),
                "error": s.error,
            }
            for s in self.states.values()
        ]


