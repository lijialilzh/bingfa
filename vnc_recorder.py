#!/usr/bin/env python3
"""
VNC 录制模块：在服务器上启动虚拟显示 + 浏览器，通过 noVNC 把浏览器画面
实时推送到网页，用户在任何设备上都能直接看到并操作浏览器。

依赖（服务器上安装）：
  sudo apt install -y xvfb x11vnc
  pip install websockify
  noVNC 静态文件放在 novnc/ 目录（见 install_vnc.sh）

流程：
  1. 启动 Xvfb 虚拟显示（:99）
  2. 启动 x11vnc 把虚拟显示暴露为 VNC
  3. 启动 websockify 把 VNC 桥接为 WebSocket
  4. 用 Playwright 在虚拟显示上启动浏览器
  5. 网页通过 noVNC 客户端连接 WebSocket，看到并操作浏览器
  6. Playwright 捕获所有网络请求
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent
NOVNC_DIR = BASE_DIR / "novnc"


def to_password_hash(pwd: str) -> str:
    pwd = pwd.strip()
    if len(pwd) == 64 and all(c in "0123456789abcdefABCDEF" for c in pwd):
        return pwd.lower()
    return hashlib.sha256(pwd.encode()).hexdigest()


class VNCRecorder:
    """VNC 录制器：虚拟显示 + 浏览器 + noVNC 桥接。"""

    def __init__(self, emit: Optional[Callable[[dict], None]] = None):
        self.emit = emit
        self.requests: list[dict] = []
        self._stop = False
        self._browser = None
        self._context = None
        self._page = None
        # 子进程
        self._xvfb_proc = None
        self._x11vnc_proc = None
        self._websockify_proc = None
        self.display = ":99"
        self.vnc_port = 5900
        self.ws_port = 6080

    def stop(self) -> None:
        self._stop = True

    def _emit(self, event: dict) -> None:
        if self.emit:
            self.emit(event)

    def _start_xvfb(self) -> None:
        """启动虚拟显示。"""
        if shutil.which("Xvfb") is None:
            raise RuntimeError("未安装 Xvfb，请执行：sudo apt install -y xvfb")
        # 若已有 Xvfb 在跑，先杀掉
        subprocess.run(["pkill", "-f", f"Xvfb {self.display}"],
                       capture_output=True)
        time.sleep(0.5)
        self._xvfb_proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", "1440x900x24",
             "-ac", "+extension", "RANDR"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    def _start_x11vnc(self) -> None:
        """启动 x11vnc，把虚拟显示暴露为 VNC。"""
        if shutil.which("x11vnc") is None:
            raise RuntimeError("未安装 x11vnc，请执行：sudo apt install -y x11vnc")
        self._x11vnc_proc = subprocess.Popen(
            ["x11vnc", "-display", self.display, "-forever",
             "-shared", "-nopw", "-rfbport", str(self.vnc_port),
             "-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    def _start_websockify(self) -> None:
        """启动 websockify，把 VNC 桥接为 WebSocket。"""
        if shutil.which("websockify") is None:
            raise RuntimeError("未安装 websockify，请执行：pip install websockify")
        self._websockify_proc = subprocess.Popen(
            ["websockify", str(self.ws_port),
             f"127.0.0.1:{self.vnc_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    def _cleanup_procs(self) -> None:
        for proc in (self._websockify_proc, self._x11vnc_proc, self._xvfb_proc):
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._websockify_proc = None
        self._x11vnc_proc = None
        self._xvfb_proc = None

    async def run(self, url: str, login_first: bool = False,
                  base_url: str = "", account: str = "test",
                  password: str = "123qwe",
                  login_api_path: str = "/api/v1/user/login") -> list[dict]:
        """启动虚拟显示 + 浏览器，打开 url，捕获请求，直到 stop() 被调用。"""
        self.requests = []
        self._stop = False

        # 启动虚拟显示和 VNC 桥接
        self._start_xvfb()
        self._start_x11vnc()
        self._start_websockify()
        self._emit({"type": "vnc_ready", "ws_port": self.ws_port,
                    "ts": time.time()})

        os.environ["DISPLAY"] = self.display

        try:
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
                    except Exception as e:
                        self._emit({"type": "record_error",
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
                        self._emit({"type": "record_request",
                                    "method": req.method,
                                    "url": req.url,
                                    "ts": time.time()})

                self._page.on("request", lambda req: asyncio.create_task(on_request(req)))

                try:
                    await self._page.goto(url, wait_until="domcontentloaded",
                                          timeout=60000)
                except Exception as e:
                    self._emit({"type": "record_error",
                                "msg": f"打开页面失败: {type(e).__name__}: {e}",
                                "ts": time.time()})

                # 等待停止信号
                while not self._stop:
                    await asyncio.sleep(0.3)

                await self._browser.close()
                self._browser = None
                self._context = None
                self._page = None
        finally:
            self._cleanup_procs()
            os.environ.pop("DISPLAY", None)
        return self.requests
