#!/usr/bin/env python3
"""
直接访问阅片页面，测试页面加载时触发的所有「下载图像」接口。

流程：
  1. 通过 API 登录，把 cookie 注入浏览器上下文
  2. 直接打开阅片页 /app/txzq/view/{study_uid}
  3. 监听页面发出的所有请求/响应，按接口分类统计：
     - 登录            POST /api/v1/user/login
     - 图像元数据      GET  /api/v1/studies
     - 序列数据        GET  /api/repacs/series/{uid}/dcp
     - 缩略图          GET  /RESULT/thumbnail/{uid}/thumbnail.jpg
     - 图像帧          GET  /xa_brain_encrypt/...
  4. 等待图像帧加载完成（或超时），输出每个接口的请求数、成功/失败、耗时

用法：
    .venv/bin/python test_page_download.py
    .venv/bin/python test_page_download.py --observe 30   # 观察 30 秒
    .venv/bin/python test_page_download.py --headed       # 显示浏览器窗口
"""

import argparse
import asyncio
import hashlib
import json
import time
from collections import defaultdict

import httpx
from playwright.async_api import async_playwright

BASE_URL = "http://192.168.108.109:8000"
ACCOUNT = "test"
PASSWORD = "123qwe"
STUDY_UID = "1.3.46.670589.28.68172260368172520200518000449629596"
PRODUCT = "XA_BRAIN"
TASK_TYPE = "xa_brain"

# 接口分类：前缀 -> 标签
API_TAGS = [
    ("/api/v1/user/login", "登录"),
    ("/api/v1/studies", "图像元数据"),
    ("/api/repacs/series", "序列数据"),
    ("/RESULT/thumbnail", "缩略图"),
    ("/xa_brain_encrypt/", "图像帧"),
]


def tag_of(url: str) -> str:
    for prefix, label in API_TAGS:
        if prefix in url:
            return label
    return "其他"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


async def main() -> None:
    parser = argparse.ArgumentParser(description="直接访问页面测试下载图像接口")
    parser.add_argument("--observe", type=float, default=30.0,
                        help="页面加载后观察图像帧加载的秒数，默认 30")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--base-url", default=BASE_URL, help="产品地址")
    parser.add_argument("--account", default=ACCOUNT, help="账号")
    parser.add_argument("--password", default=PASSWORD, help="密码")
    parser.add_argument("--study-uid", default=STUDY_UID, help="studyInstanceUID")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    viewer_url = f"{base}/app/txzq/view/{args.study_uid}"

    # 统计结构：tag -> {requests, ok, fail, times[], sizes[], first_ts}
    stats = defaultdict(lambda: {"requests": 0, "ok": 0, "fail": 0,
                                 "times": [], "sizes": [], "first_ts": 0.0})
    total_frames = 0

    print("=" * 72)
    print(f"直接访问页面测试下载图像接口")
    print(f"目标: {base}   账号: {args.account}   headless={not args.headed}")
    print("=" * 72)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
        )

        # ---- 1. API 登录，注入 cookie ----
        print("\n[1] 登录（API 注入 cookie）")
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base}/api/v1/user/login",
                json={"name": args.account, "password": sha256(args.password)})
            data = resp.json()
            if data.get("code") != 10000:
                print(f"  ❌ 登录失败: {resp.text[:200]}")
                await browser.close()
                return
            cookies = [{"name": k, "value": v,
                        "domain": "192.168.108.109", "path": "/"}
                       for k, v in resp.cookies.items()]
            await ctx.add_cookies(cookies)
        print(f"  ✅ 登录成功  {fmt_ms((time.perf_counter() - t0) * 1000)}  "
              f"userId={data.get('data', {}).get('userId')}")

        page = await ctx.new_page()

        # ---- 监听请求/响应 ----
        async def on_request(req):
            tag = tag_of(req.url)
            if tag == "其他":
                return
            s = stats[tag]
            s["requests"] += 1
            if s["first_ts"] == 0.0:
                s["first_ts"] = time.time()

        async def on_response(res):
            tag = tag_of(res.url)
            if tag == "其他":
                return
            s = stats[tag]
            s["times"].append((time.perf_counter() - t_start) * 1000)
            if res.status < 400:
                s["ok"] += 1
                try:
                    body = await res.body()
                    s["sizes"].append(len(body))
                except Exception:
                    pass
            else:
                s["fail"] += 1
                print(f"  ⚠️ {tag} HTTP {res.status} {res.url[:100]}")

        page.on("request", lambda req: asyncio.create_task(on_request(req)))
        page.on("response", lambda res: asyncio.create_task(on_response(res)))

        # ---- 2. 直接打开阅片页 ----
        print("\n[2] 打开阅片页")
        t_start = time.perf_counter()
        t0 = time.perf_counter()
        try:
            await page.goto(viewer_url, wait_until="domcontentloaded",
                            timeout=120000)
            print(f"  ✅ 页面加载完成 (domcontentloaded)  "
                  f"{fmt_ms((time.perf_counter() - t0) * 1000)}")
        except Exception as e:
            print(f"  ❌ 页面加载失败: {type(e).__name__}: {e}")
            await browser.close()
            return

        # ---- 3. 观察图像帧加载 ----
        print(f"\n[3] 观察图像帧加载（{args.observe:.0f} 秒）")
        deadline = time.perf_counter() + args.observe
        last_count = -1
        while time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            frames = stats["图像帧"]["requests"]
            if frames != last_count:
                elapsed = time.perf_counter() - t_start
                print(f"  {elapsed:5.1f}s  已请求 {frames} 帧")
                last_count = frames
            # 若已知总帧数且已全部请求，提前结束
            if total_frames > 0 and frames >= total_frames:
                break

        await browser.close()

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("页面加载触发的接口统计")
    print("=" * 72)
    order = ["登录", "图像元数据", "序列数据", "缩略图", "图像帧"]
    for tag in order:
        s = stats[tag]
        if s["requests"] == 0:
            print(f"  ⬜ {tag:<8} 未触发")
            continue
        times = s["times"]
        avg = sum(times) / len(times) if times else 0
        total_size = sum(s["sizes"])
        size_str = (f"{total_size / 1024 / 1024:.2f} MB"
                    if total_size >= 1024 * 1024
                    else f"{total_size / 1024:.1f} KB")
        print(f"  {'✅' if s['fail'] == 0 else '❌'} {tag:<8} "
              f"请求 {s['requests']:>4} 次, 成功 {s['ok']:>4}, 失败 {s['fail']:>2}, "
              f"avg={fmt_ms(avg)}, 总大小 {size_str}")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
