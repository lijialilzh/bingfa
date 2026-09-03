#!/usr/bin/env python3
"""
并发测试：模拟 N 个用户端（不同笔记本 + Chrome）同时登录并进入图像工作站阅片。

每个用户使用独立的浏览器上下文（隔离 cookie / localStorage / 缓存），
等价于 N 台不同笔记本各自打开 Chrome 访问产品。

流程（两阶段，全部同时触发）：
  阶段1  所有用户同时登录
  阶段2  所有用户同时进入阅片页并加载图像帧

用法：
    .venv/bin/python concurrency_test.py --users 100
    .venv/bin/python concurrency_test.py --users 100 --headed   # 显示真实浏览器窗口
    .venv/bin/python concurrency_test.py --users 50 --observe 20
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass

from playwright.async_api import async_playwright

# ---------------- 配置 ----------------
BASE_URL = "http://192.168.108.109:8000"
LOGIN_URL = f"{BASE_URL}/app/center/login"
USERNAME = "test"
PASSWORD = "123qwe"
# 阅片目标（检查列表里 XA-00001 的 studyInstanceUID）
STUDY_UID = "1.3.46.670589.28.68172260368172520200518000449629596"
VIEWER_URL = f"{BASE_URL}/app/txzq/view/{STUDY_UID}"
# 图像帧接口前缀（用于统计图像加载）
FRAME_PREFIX = "/xa_brain_encrypt/"


@dataclass
class UserResult:
    user_id: int
    login_ok: bool = False
    login_ms: float = 0.0
    viewer_ok: bool = False
    viewer_ms: float = 0.0          # 进入阅片页到页面 domcontentloaded 耗时
    frames_loaded: int = 0          # 观察窗口内加载的图像帧数
    first_frame_ms: float = 0.0     # 进入阅片页到首帧耗时
    error: str = ""


async def login_and_view(user_id: int, context, login_barrier: asyncio.Barrier,
                         viewer_barrier: asyncio.Barrier,
                         observe_seconds: float) -> UserResult:
    """单个用户：登录 -> 等待 -> 进入阅片 -> 观察图像加载。"""
    res = UserResult(user_id=user_id)
    page = await context.new_page()

    frame_times = []
    page.on("request", lambda req: frame_times.append(time.perf_counter())
            if FRAME_PREFIX in req.url else None)

    try:
        # ---- 阶段1：登录 ----
        await login_barrier.wait()          # 所有用户同时开始登录
        t0 = time.perf_counter()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.fill('input[placeholder*="用户名"], input[type="text"]', USERNAME)
        await page.fill('input[type="password"]', PASSWORD)
        await page.click('button:has-text("登 录")')
        try:
            await page.wait_for_url("**/ground/checkList**", timeout=30000)
            res.login_ok = True
        except Exception:
            await page.wait_for_timeout(2000)
            res.login_ok = "checkList" in page.url
        res.login_ms = (time.perf_counter() - t0) * 1000

        if not res.login_ok:
            res.error = "登录失败"
            return res

        # ---- 阶段2：所有用户同时进入阅片 ----
        await viewer_barrier.wait()
        t1 = time.perf_counter()
        frame_times.clear()
        await page.goto(VIEWER_URL, wait_until="domcontentloaded", timeout=30000)
        res.viewer_ok = True
        res.viewer_ms = (time.perf_counter() - t1) * 1000

        # 观察图像帧加载
        await page.wait_for_timeout(observe_seconds * 1000)
        res.frames_loaded = len(frame_times)
        if frame_times:
            res.first_frame_ms = (frame_times[0] - t1) * 1000
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    finally:
        await context.close()
    return res


def summarize(results: list[UserResult], total_ms: float) -> None:
    n = len(results)
    ok_login = [r for r in results if r.login_ok]
    ok_viewer = [r for r in results if r.viewer_ok]
    errs = [r for r in results if r.error]

    def pct(vals, p):
        if not vals:
            return 0.0
        return statistics.quantiles(vals, n=100, method="inclusive")[p - 1]

    print("\n" + "=" * 64)
    print(f"并发测试结果汇总  (总耗时 {total_ms/1000:.1f}s)")
    print("=" * 64)
    print(f"用户总数        : {n}")
    print(f"登录成功        : {len(ok_login)} / {n}")
    print(f"进入阅片成功    : {len(ok_viewer)} / {n}")
    print(f"出错用户        : {len(errs)}")

    if ok_login:
        vals = [r.login_ms for r in ok_login]
        print(f"\n[登录耗时 ms]   avg={statistics.mean(vals):.0f}  "
              f"min={min(vals):.0f}  max={max(vals):.0f}  "
              f"p50={pct(vals,50):.0f}  p95={pct(vals,95):.0f}  p99={pct(vals,99):.0f}")

    if ok_viewer:
        vals = [r.viewer_ms for r in ok_viewer]
        print(f"[阅片页加载 ms] avg={statistics.mean(vals):.0f}  "
              f"min={min(vals):.0f}  max={max(vals):.0f}  "
              f"p50={pct(vals,50):.0f}  p95={pct(vals,95):.0f}  p99={pct(vals,99):.0f}")

        frames = [r.frames_loaded for r in ok_viewer]
        print(f"[图像帧加载数]  avg={statistics.mean(frames):.0f}  "
              f"min={min(frames)}  max={max(frames)}")

        firsts = [r.first_frame_ms for r in ok_viewer if r.first_frame_ms > 0]
        if firsts:
            print(f"[首帧耗时 ms]    avg={statistics.mean(firsts):.0f}  "
                  f"min={min(firsts):.0f}  max={max(firsts):.0f}  "
                  f"p95={pct(firsts,95):.0f}")

    if errs:
        print(f"\n[错误明细] (前 20 条)")
        for r in errs[:20]:
            print(f"  user {r.user_id}: {r.error}")

    out = {
        "total_ms": total_ms,
        "users": n,
        "login_ok": len(ok_login),
        "viewer_ok": len(ok_viewer),
        "errors": len(errs),
        "results": [
            {
                "user_id": r.user_id,
                "login_ok": r.login_ok,
                "login_ms": round(r.login_ms, 1),
                "viewer_ok": r.viewer_ok,
                "viewer_ms": round(r.viewer_ms, 1),
                "frames_loaded": r.frames_loaded,
                "first_frame_ms": round(r.first_frame_ms, 1),
                "error": r.error,
            }
            for r in results
        ],
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n明细已保存到 result.json")


async def main() -> None:
    parser = argparse.ArgumentParser(description="图像工作站阅片并发测试")
    parser.add_argument("--users", type=int, default=100, help="并发用户数")
    parser.add_argument("--observe", type=float, default=15.0,
                        help="进入阅片后观察图像加载的秒数")
    parser.add_argument("--headed", action="store_true", help="显示真实浏览器窗口(默认无头)")
    parser.add_argument("--context-batch", type=int, default=20,
                        help="每批创建的上下文数(控制内存峰值)")
    args = parser.parse_args()

    headless = not args.headed
    print(f"目标: {BASE_URL}")
    print(f"并发用户数: {args.users}  观察窗口: {args.observe}s  "
          f"headless={headless}")

    t_start = time.perf_counter()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # ---- 阶段0：分批创建所有独立上下文（模拟不同笔记本） ----
        contexts = []
        for i in range(0, args.users, args.context_batch):
            chunk = list(range(i, min(i + args.context_batch, args.users)))
            for uid in chunk:
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                )
                contexts.append((uid, ctx))
            print(f"  已创建 {len(contexts)}/{args.users} 个独立上下文")

        login_barrier = asyncio.Barrier(args.users)
        viewer_barrier = asyncio.Barrier(args.users)

        tasks = [
            login_and_view(uid, ctx, login_barrier, viewer_barrier, args.observe)
            for uid, ctx in contexts
        ]
        results = await asyncio.gather(*tasks)
        await browser.close()

    total_ms = (time.perf_counter() - t_start) * 1000
    summarize(results, total_ms)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
