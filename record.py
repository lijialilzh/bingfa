#!/usr/bin/env python3
"""
录制模块：用 Playwright 打开真实浏览器，用户手动操作，
捕获所有 HTTP 请求（方法、URL、请求头、请求体），
停止后返回捕获的接口列表，可导入到多接口测试。

支持从任意地址开始录制；可选「先登录」：先用账号密码登录拿到 cookie
注入浏览器，再打开目标地址（这样输入阅片地址也能直接进入阅片页）。
"""

import asyncio
import hashlib
import json
import time
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright


def to_password_hash(pwd: str) -> str:
    pwd = pwd.strip()
    if len(pwd) == 64 and all(c in "0123456789abcdefABCDEF" for c in pwd):
        return pwd.lower()
    return hashlib.sha256(pwd.encode()).hexdigest()


class Recorder:
    """浏览器录制器。"""

    def __init__(self, emit: Optional[Callable[[dict], None]] = None):
        self.emit = emit
        self.requests: list[dict] = []
        self._stop = False
        self._browser = None
        self._context = None
        self._page = None

    def stop(self) -> None:
        self._stop = True

    async def run(self, url: str, login_first: bool = False,
                  base_url: str = "", account: str = "test",
                  password: str = "123qwe",
                  login_api_path: str = "/api/v1/user/login") -> list[dict]:
        """启动浏览器，打开 url，捕获请求，直到 stop() 被调用。"""
        self.requests = []
        self._stop = False
        async with async_playwright() as p:
            self._browser = await p.chromium.launch(headless=False)
            self._context = await self._browser.new_context(
                viewport={"width": 1440, "height": 900})
            self._page = await self._context.new_page()

            # 可选：先登录，注入 cookie
            if login_first and base_url:
                try:
                    pwd_hash = to_password_hash(password)
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(
                            base_url.rstrip("/") + login_api_path,
                            json={"name": account, "password": pwd_hash})
                        data = resp.json()
                        if data.get("code") == 10000:
                            cookies = []
                            for name, value in resp.cookies.items():
                                cookies.append({"name": name, "value": value,
                                                "domain": base_url.split("//")[1].split(":")[0],
                                                "path": "/"})
                            uid = data.get("data", {}).get("userId")
                            if uid is not None:
                                cookies.append({"name": "userId", "value": str(uid),
                                                "domain": base_url.split("//")[1].split(":")[0],
                                                "path": "/"})
                                cookies.append({"name": "name", "value": account,
                                                "domain": base_url.split("//")[1].split(":")[0],
                                                "path": "/"})
                            if cookies:
                                await self._context.add_cookies(cookies)
                            if self.emit:
                                self.emit({"type": "record_request",
                                           "method": "POST",
                                           "url": base_url.rstrip("/") + login_api_path,
                                           "ts": time.time()})
                except Exception as e:
                    if self.emit:
                        self.emit({"type": "record_error",
                                   "msg": f"预登录失败: {type(e).__name__}: {e}",
                                   "ts": time.time()})

            async def on_request(req):
                if req.resource_type in ("document", "xhr", "fetch"):
                    try:
                        body = req.post_data or ""
                    except Exception:
                        body = ""
                    rec = {
                        "method": req.method,
                        "url": req.url,
                        "headers": dict(req.headers),
                        "body": body,
                        "ts": time.time(),
                    }
                    self.requests.append(rec)
                    if self.emit:
                        self.emit({"type": "record_request",
                                   "method": req.method,
                                   "url": req.url,
                                   "ts": time.time()})

            self._page.on("request", lambda req: asyncio.create_task(on_request(req)))

            try:
                await self._page.goto(url, wait_until="domcontentloaded",
                                      timeout=60000)
            except Exception as e:
                if self.emit:
                    self.emit({"type": "record_error",
                               "msg": f"打开页面失败: {type(e).__name__}: {e}",
                               "ts": time.time()})

            # 等待停止信号
            while not self._stop:
                await asyncio.sleep(0.3)

            await self._browser.close()
            self._browser = None
            self._context = None
            self._page = None
        return self.requests

