#!/usr/bin/env python3
"""
真实浏览器并发测试：用 Playwright 真实浏览器模拟 N 个用户同时登录并打开阅片界面。

每个用户使用独立的浏览器上下文（隔离 cookie / localStorage / 缓存），
等价于 N 台不同笔记本各自打开 Chrome 访问产品。

与 engine.py 的纯 HTTP 压测不同，这里用真实浏览器渲染页面、执行 JS、
加载图像，最接近真实用户操作。

流程（两阶段，全部同时触发）：
  阶段1  所有用户同时登录
  阶段2  所有用户同时进入阅片页并加载图像
"""

import asyncio
import hashlib
import time
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright


class BrowserConcurrencyRunner:
    """真实浏览器并发测试执行器。"""

    def __init__(self, emit: Callable[[dict], None],
                 config: Optional[dict] = None):
        self.emit = emit
        self._stop = False
        cfg = config or {}
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.login_path = cfg.get("login_path") or "/app/center/login"
        self.password = cfg.get("password") or "123qwe"
        self.study_uid = cfg.get("study_uid") or ""
        self.accounts = cfg.get("accounts") or []
        # 阅片 URL 前缀 / 帧前缀（product 小写），启动时动态解析
        self.viewer_prefix = ""
        self.frame_prefix = ""
        self._total_frames = 0

    def stop(self) -> None:
        self._stop = True

    def _emit(self, event: dict) -> None:
        if self.emit:
            self.emit(event)

    async def _resolve_viewer_info(self) -> tuple[str, str, str, int]:
        """通过检查列表 API 解析阅片地址、帧前缀、序列 UID 和总张数。

        返回 (viewer_prefix, frame_prefix, study_uid, total_frames)。
        """
        account = self.accounts[0] if self.accounts else "test"
        pwd_hash = hashlib.sha256(self.password.encode()).hexdigest()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            # 登录
            resp = await client.post(
                self.base_url + "/api/v1/user/login",
                json={"name": account, "password": pwd_hash})
            data = resp.json()
            if data.get("code") != 10000:
                raise RuntimeError(f"预登录失败: {data.get('msg', data)}")
            # 检查列表（POST 分页查询）
            resp = await client.post(
                self.base_url + "/api/v1/studies/query/online",
                json={"order_by": [{"studyDate": "desc"}],
                      "page": {"no": 1, "length": 100}})
            data = resp.json()
            items = (data.get("data") or {}).get("items") or []
            if not items:
                raise RuntimeError("检查列表为空")

            # 找到匹配 study_uid 的检查，否则用第一个
            target = None
            for item in items:
                if item.get("studyInstanceUID") == self.study_uid:
                    target = item
                    break
            if target is None:
                target = items[0]

            study_uid = target.get("studyInstanceUID", "")
            series_uid = ""
            product = ""
            for series in target.get("series", []):
                products = series.get("product") or []
                if products:
                    product = products[0].lower()
                    series_uid = series.get("seriesInstanceUID", "")
                    break

            if not product:
                raise RuntimeError("无法识别产品类型")

            # 通过 dcp 获取总张数
            total_frames = 0
            if series_uid:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/repacs/series/{series_uid}/dcp")
                    images = resp.json().get("images", [])
                    total_frames = len(images)
                except Exception:
                    total_frames = 0

            return (f"/app/{product}/view/",
                    f"/{product}_encrypt/",
                    study_uid, total_frames)

    async def _login_and_view(self, user_id: int, account: str, context,
                              login_barrier: asyncio.Barrier,
                              viewer_barrier: asyncio.Barrier,
                              observe_seconds: float,
                              viewer_url: str) -> dict:
        """单个用户：登录 -> 等待 -> 进入阅片 -> 观察图像加载。"""
        res = {
            "user_id": user_id,
            "account": account,
            "login_ok": False,
            "login_ms": 0.0,
            "viewer_ok": False,
            "viewer_ms": 0.0,
            "frames_loaded": 0,
            "first_frame_ms": 0.0,
            "error": "",
        }
        page = await context.new_page()
        frame_times = []

        def on_frame_request(req):
            frame_times.append(time.perf_counter())
            self._emit({"type": "request", "user_id": user_id,
                        "account": account, "method": req.method,
                        "url": req.url, "tag": "图像帧", "ts": time.time()})

        page.on("request", lambda req: on_frame_request(req)
                if self.frame_prefix in req.url else None)

        try:
            # ---- 阶段1：登录 ----
            try:
                await asyncio.wait_for(login_barrier.wait(), timeout=60)
            except Exception:
                pass
            t0 = time.perf_counter()
            await page.goto(self.base_url + self.login_path,
                            wait_until="domcontentloaded", timeout=30000)
            await page.fill('input[placeholder="用户名"]', account)
            await page.fill('input[placeholder="密码"]', self.password)
            await page.click('button:has-text("登 录")')
            try:
                await page.wait_for_url("**/ground/checkList**", timeout=30000)
                res["login_ok"] = True
            except Exception:
                await page.wait_for_timeout(2000)
                res["login_ok"] = "checkList" in page.url
            res["login_ms"] = (time.perf_counter() - t0) * 1000

            if res["login_ok"]:
                self._emit({"type": "login_ok", "user_id": user_id,
                            "account": account, "login_ms": res["login_ms"],
                            "ts": time.time()})
                self._emit({"type": "user_status", "user_id": user_id,
                            "account": account, "status": "viewer",
                            "ts": time.time()})
            else:
                res["error"] = "登录失败"
                self._emit({"type": "user_status", "user_id": user_id,
                            "account": account, "status": "error",
                            "error": res["error"], "ts": time.time()})
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
            self._emit({"type": "user_status", "user_id": user_id,
                        "account": account, "status": "error",
                        "error": res["error"], "ts": time.time()})
        finally:
            # 无论登录成败，都到达 viewer barrier，避免卡住其他用户
            try:
                await asyncio.wait_for(viewer_barrier.wait(), timeout=60)
            except Exception:
                pass

        if not res["login_ok"]:
            await context.close()
            return res

        # ---- 阶段2：所有用户同时进入阅片 ----
        try:
            t1 = time.perf_counter()
            frame_times.clear()
            await page.goto(viewer_url, wait_until="domcontentloaded",
                            timeout=30000)
            res["viewer_ok"] = True
            res["viewer_ms"] = (time.perf_counter() - t1) * 1000
            self._emit({"type": "user_status", "user_id": user_id,
                        "account": account, "status": "loading",
                        "ts": time.time()})

            # 观察图像帧加载（可被 stop 中断）
            elapsed = 0.0
            while elapsed < observe_seconds:
                if self._stop:
                    break
                await asyncio.sleep(0.5)
                elapsed += 0.5
            res["frames_loaded"] = len(frame_times)
            if frame_times:
                res["first_frame_ms"] = (frame_times[0] - t1) * 1000
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
        finally:
            await context.close()

        status = "done" if res["viewer_ok"] else (
            "stopped" if self._stop else "error")
        self._emit({"type": "user_done", "user_id": user_id,
                    "account": account, "frames_loaded": res["frames_loaded"],
                    "total_frames": self._total_frames,
                    "all_frames_ms": 0.0,
                    "status": status, "ts": time.time()})
        return res

    async def run(self, users: int, observe_seconds: float,
                  headless: bool = True) -> list[dict]:
        """启动真实浏览器并发测试。"""
        self._stop = False
        accounts = (self.accounts[:users] if self.accounts
                    else [f"user{i + 1}" for i in range(users)])

        # 解析阅片地址
        self._emit({"type": "progress",
                    "msg": "准备：解析阅片地址 ...", "ts": time.time()})
        try:
            (self.viewer_prefix, self.frame_prefix,
             study_uid, total_frames) = await self._resolve_viewer_info()
        except Exception as e:
            self._emit({"type": "error",
                        "msg": f"解析阅片地址失败: {type(e).__name__}: {e}",
                        "ts": time.time()})
            return []
        self._total_frames = total_frames
        viewer_url = self.base_url + self.viewer_prefix + study_uid
        self._emit({"type": "progress",
                    "msg": f"阅片地址: {viewer_url}（共 {total_frames} 张）",
                    "ts": time.time()})

        self._emit({"type": "start", "users": users, "accounts": accounts,
                    "total_frames": total_frames, "mode": "browser",
                    "ts": time.time()})

        t_start = time.perf_counter()
        results = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                # 分批创建独立上下文（模拟不同笔记本）
                contexts = []
                for uid, account in enumerate(accounts):
                    ctx = await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
                    )
                    contexts.append((uid, account, ctx))
                    self._emit({"type": "user_status", "user_id": uid,
                                "account": account, "status": "pending",
                                "ts": time.time()})

                login_barrier = asyncio.Barrier(users)
                viewer_barrier = asyncio.Barrier(users)

                tasks = [
                    self._login_and_view(uid, account, ctx, login_barrier,
                                         viewer_barrier, observe_seconds,
                                         viewer_url)
                    for uid, account, ctx in contexts
                ]
                results = await asyncio.gather(*tasks)
                await browser.close()
        except Exception as e:
            self._emit({"type": "error",
                        "msg": f"浏览器启动失败: {type(e).__name__}: {e}",
                        "ts": time.time()})

        # 汇总
        total_ms = (time.perf_counter() - t_start) * 1000
        states = []
        for r in results:
            states.append({
                "user_id": r["user_id"],
                "account": r["account"],
                "status": ("done" if r["viewer_ok"] else
                           ("error" if r["error"] else "timeout")),
                "login_ms": r["login_ms"],
                "viewer_ms": r["viewer_ms"],
                "first_frame_ms": r["first_frame_ms"],
                "all_frames_ms": 0.0,
                "frames_loaded": r["frames_loaded"],
                "total_frames": total_frames,
                "error": r["error"],
            })
        self._emit({"type": "summary", "states": states,
                    "browser_results": results, "total_ms": total_ms,
                    "ts": time.time()})
        return results
