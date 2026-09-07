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
    "studies_api_path": "/api/v1/studies",
    "dcp_api_path_template": "/api/repacs/series/{series_uid}/dcp",
    "frame_prefix": "/xa_brain_encrypt/",
    "thumbnail_prefix": "/RESULT/thumbnail",
    "msg_token_prefix": "/api/v1/msg/token",
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
    # ---- 可审计证据 ----
    session_token: str = ""          # 登录后服务器下发的独立会话 token
    user_id_server: str = ""         # 服务器返回的 userId
    login_ts: float = 0.0            # 登录请求发出的绝对时间戳(epoch 秒)
    viewer_ts: float = 0.0           # 进入阅片的绝对时间戳
    first_frame_ts: float = 0.0      # 首帧请求的绝对时间戳
    error: str = ""


class TestEngine:
    """纯 HTTP 接口并发压测，事件通过 emit 回调推送。"""

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
        self.default_password = cfg["password"]
        self.study_uid = cfg["study_uid"]
        self.series_uid = cfg.get("series_uid", "")
        # ---- 接口路径（可配置，换产品时修改）----
        self.login_api_path = cfg.get("login_api_path", "/api/v1/user/login")
        self.check_login_prefix = cfg.get("check_login_prefix", "/api/v1/user/checkLogin")
        self.studies_api_path = cfg.get("studies_api_path", "/api/v1/studies")
        self.dcp_api_path_template = cfg.get(
            "dcp_api_path_template", "/api/repacs/series/{series_uid}/dcp")
        self.frame_prefix = cfg.get("frame_prefix", "/xa_brain_encrypt/")
        self.thumbnail_prefix = cfg.get("thumbnail_prefix", "/RESULT/thumbnail")
        self.msg_token_prefix = cfg.get("msg_token_prefix", "/api/v1/msg/token")
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
        """按录制导入的接口列表循环压测。"""
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
        deadline = t0 + self.observe_seconds

        def fill(text: str) -> str:
            return (text
                    .replace("{account}", account)
                    .replace("{password_hash}", pwd_hash)
                    .replace("{study_uid}", self.study_uid)
                    .replace("{series_uid}", self.series_uid))

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            idx = 0
            while time.perf_counter() < deadline and not self._stop:
                api = self.custom_apis[idx % len(self.custom_apis)]
                idx += 1
                name = api.get("name") or api.get("url", "接口")
                method = (api.get("method") or "GET").upper()
                url = fill(api.get("url", ""))
                body = fill(api.get("body", ""))
                content_type = api.get("content_type") or "application/json"
                if not url:
                    continue
                if url.startswith("/"):
                    url = self.base_url + url
                t_req = time.perf_counter()
                await self._emit_req(user_id, account, method, url, name)
                try:
                    if method == "POST":
                        if content_type == "application/x-www-form-urlencoded":
                            resp = await client.post(url, data=body,
                                                     headers={"Content-Type": content_type})
                        else:
                            resp = await client.post(url, content=body,
                                                     headers={"Content-Type": content_type})
                    else:
                        resp = await client.get(url)
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    await self._emit_resp(user_id, account, resp.status_code,
                                          url, name, elapsed)
                except Exception as e:
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    await self._emit_resp(user_id, account, 0, url, name,
                                          elapsed, f"{type(e).__name__}: {e}")

        st.all_frames_ms = (time.perf_counter() - t0) * 1000
        st.status = "done"
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
        """只压测图像帧接口：先获取帧列表，再循环下载。"""
        self.emit({"type": "progress",
                   "msg": "准备：获取图像帧列表 (dcp) ...", "ts": time.time()})
        dcp_url = self.base_url + self.dcp_api_path_template.format(
            series_uid=self.series_uid)
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
        deadline = t0 + self.observe_seconds

        async with httpx.AsyncClient(timeout=120) as client:
            idx = 0
            while time.perf_counter() < deadline and not self._stop:
                if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
                    break
                url = frame_urls[idx % len(frame_urls)]
                idx += 1
                t_req = time.perf_counter()
                await self._emit_req(user_id, st.account, "GET", url, "图像帧")
                try:
                    resp = await client.get(url)
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    if resp.status_code == 200:
                        st.frames_loaded += 1
                        if st.first_frame_ts == 0.0:
                            st.first_frame_ts = time.time()
                            st.first_frame_ms = (t_req - t0) * 1000
                    await self._emit_resp(user_id, st.account, resp.status_code,
                                          url, "图像帧", elapsed)
                except Exception as e:
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    await self._emit_resp(user_id, st.account, 0, url, "图像帧",
                                          elapsed, f"{type(e).__name__}: {e}")

        st.all_frames_ms = (time.perf_counter() - t0) * 1000
        if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
            st.status = "done"
        else:
            st.status = "timeout"
        self.emit({"type": "user_done", "user_id": user_id,
                   "account": st.account, "frames_loaded": st.frames_loaded,
                   "total_frames": st.total_frames,
                   "all_frames_ms": st.all_frames_ms,
                   "status": st.status, "ts": time.time()})

    async def _emit_req(self, user_id: int, account: str, method: str,
                        url: str, tag: str) -> None:
        self.emit({"type": "request", "user_id": user_id, "account": account,
                   "method": method, "url": url, "tag": tag, "ts": time.time()})

    async def _emit_resp(self, user_id: int, account: str, status: int,
                         url: str, tag: str, elapsed_ms: float,
                         error: str = "") -> None:
        ev = {"type": "response", "user_id": user_id, "account": account,
              "status": status, "url": url, "tag": tag,
              "elapsed_ms": elapsed_ms, "ts": time.time()}
        if error:
            ev["error"] = error
        self.emit(ev)

    async def _run_user(self, user_id: int, cred: dict) -> None:
        st = self.states[user_id]
        account = cred["account"]
        raw_pwd = cred.get("password") or self.default_password
        pwd_hash = to_password_hash(raw_pwd)

        st.status = "login"
        self.emit({"type": "user_status", "user_id": user_id,
                   "account": account, "status": "login", "ts": time.time()})

        t0 = time.perf_counter()
        t_login_ok = 0.0
        deadline = t0 + self.observe_seconds

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
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
                data = resp.json()
                if data.get("code") == 10000:
                    st.session_token = resp.cookies.get("token", "")
                    st.user_id_server = str(data.get("data", {}).get("userId", ""))
                    t_login_ok = time.perf_counter()
                    st.viewer_ts = time.time()
                    await self._emit_resp(user_id, account, resp.status_code,
                                          login_url, "登录", elapsed)
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
                                          login_url, "登录", elapsed)
                    st.status = "error"
                    st.error = "登录失败"
                    self.emit({"type": "user_status", "user_id": user_id,
                               "account": account, "status": "error",
                               "error": st.error, "ts": time.time()})
                    return
            except Exception as e:
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, 0, login_url, "登录",
                                      elapsed, f"{type(e).__name__}: {e}")
                st.status = "error"
                st.error = f"{type(e).__name__}: {e}"
                self.emit({"type": "user_status", "user_id": user_id,
                           "account": account, "status": "error",
                           "error": st.error, "ts": time.time()})
                return

            # ---- 2. 会话校验 ----
            check_url = self.base_url + self.check_login_prefix
            t_req = time.perf_counter()
            await self._emit_req(user_id, account, "GET", check_url, "会话校验")
            try:
                resp = await client.get(check_url)
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, resp.status_code,
                                      check_url, "会话校验", elapsed)
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
                                      msg_token_url, "消息令牌", elapsed)
            except Exception as e:
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, 0, msg_token_url,
                                      "消息令牌", elapsed, f"{type(e).__name__}: {e}")

            # ---- 3. 图像元数据 ----
            studies_url = (self.base_url + self.studies_api_path +
                           f"?studyInstanceUID={self.study_uid}&taskType=xa_brain&product=XA_BRAIN")
            t_req = time.perf_counter()
            await self._emit_req(user_id, account, "GET", studies_url, "图像元数据")
            try:
                resp = await client.get(studies_url)
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                try:
                    data = resp.json()
                    total = 0
                    for study in data.get("data", []):
                        for series in study.get("series", []):
                            total += int(series.get("imgFrameNumber", 0))
                    if total > 0:
                        st.total_frames = total
                        self.emit({"type": "total_frames", "user_id": user_id,
                                   "account": account, "total_frames": total,
                                   "ts": time.time()})
                except Exception:
                    pass
                await self._emit_resp(user_id, account, resp.status_code,
                                      studies_url, "图像元数据", elapsed)
            except Exception as e:
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, 0, studies_url,
                                      "图像元数据", elapsed, f"{type(e).__name__}: {e}")

            # ---- 4. 序列数据 ----
            dcp_url = self.base_url + self.dcp_api_path_template.format(
                series_uid=self.series_uid)
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
                except Exception:
                    pass
                await self._emit_resp(user_id, account, resp.status_code,
                                      dcp_url, "序列数据", elapsed)
            except Exception as e:
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, 0, dcp_url, "序列数据",
                                      elapsed, f"{type(e).__name__}: {e}")

            # ---- 5. 缩略图 ----
            thumb_url = f"{self.base_url}{self.thumbnail_prefix}/{self.series_uid}/thumbnail.jpg"
            t_req = time.perf_counter()
            await self._emit_req(user_id, account, "GET", thumb_url, "缩略图")
            try:
                resp = await client.get(thumb_url)
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, resp.status_code,
                                      thumb_url, "缩略图", elapsed)
            except Exception as e:
                elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                await self._emit_resp(user_id, account, 0, thumb_url, "缩略图",
                                      elapsed, f"{type(e).__name__}: {e}")

            # ---- 6. 图像帧（循环下载，直到超时或全部加载完） ----
            if t_login_ok > 0:
                st.viewer_ms = (time.perf_counter() - t_login_ok) * 1000
            st.status = "loading"
            self.emit({"type": "user_status", "user_id": user_id,
                       "account": account, "status": "loading", "ts": time.time()})
            if not frame_urls:
                st.status = "timeout"
                self.emit({"type": "user_done", "user_id": user_id,
                           "account": account, "frames_loaded": 0,
                           "total_frames": st.total_frames,
                           "all_frames_ms": 0, "status": "timeout",
                           "ts": time.time()})
                return

            idx = 0
            while time.perf_counter() < deadline and not self._stop:
                if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
                    break
                url = frame_urls[idx % len(frame_urls)]
                idx += 1
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
                                          url, "图像帧", elapsed)
                except Exception as e:
                    elapsed = round((time.perf_counter() - t_req) * 1000, 1)
                    await self._emit_resp(user_id, account, 0, url, "图像帧",
                                          elapsed, f"{type(e).__name__}: {e}")

            st.all_frames_ms = (time.perf_counter() - t0) * 1000
            if st.total_frames > 0 and st.frames_loaded >= st.total_frames:
                st.status = "done"
            else:
                st.status = "timeout"
            self.emit({"type": "user_done", "user_id": user_id,
                       "account": account, "frames_loaded": st.frames_loaded,
                       "total_frames": st.total_frames,
                       "all_frames_ms": st.all_frames_ms,
                       "status": st.status, "ts": time.time()})

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


