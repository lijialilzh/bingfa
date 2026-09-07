#!/usr/bin/env python3
"""
本地录制脚本：在你自己的电脑（Windows/Mac）上运行。
- 本地弹出浏览器，你手动操作
- 自动捕获所有 HTTP 请求
- 操作完成后，把捕获的请求上传到测试平台服务器

用法：
  python local_record.py --server http://服务器IP:19000 --url http://192.168.108.109:8000/app/center/login

参数：
  --server  测试平台服务器地址（必填）
  --url     起始录制地址（必填）
  --login   可选，先登录再打开起始页（账号密码从服务器配置读取）
  --account 登录账号（配合 --login 使用）
  --password 登录密码（配合 --login 使用）

依赖（在你电脑上安装一次）：
  pip install playwright httpx
  playwright install chromium
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time

import httpx
from playwright.async_api import async_playwright


def to_password_hash(pwd: str) -> str:
    pwd = pwd.strip()
    if len(pwd) == 64 and all(c in "0123456789abcdefABCDEF" for c in pwd):
        return pwd.lower()
    return hashlib.sha256(pwd.encode()).hexdigest()


async def main():
    parser = argparse.ArgumentParser(description="本地录制脚本")
    parser.add_argument("--server", required=True, help="测试平台服务器地址，如 http://192.168.1.100:19000")
    parser.add_argument("--url", required=True, help="起始录制地址")
    parser.add_argument("--login", action="store_true", help="先登录再打开起始页")
    parser.add_argument("--account", default="test", help="登录账号")
    parser.add_argument("--password", default="123qwe", help="登录密码")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    url = args.url
    requests = []

    print("=" * 50)
    print("  本地录制已启动")
    print(f"  服务器: {server}")
    print(f"  起始页: {url}")
    print("  浏览器即将弹出，请手动操作")
    print("  操作完成后，回到本窗口按 Enter 结束录制")
    print("=" * 50)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # 可选：先登录注入 cookie
        if args.login:
            try:
                pwd_hash = to_password_hash(args.password)
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        server + "/api/v1/user/login",
                        json={"name": args.account, "password": pwd_hash})
                    data = resp.json()
                    if data.get("code") == 10000:
                        cookies = []
                        for name, value in resp.cookies.items():
                            cookies.append({"name": name, "value": value,
                                            "domain": url.split("//")[1].split(":")[0],
                                            "path": "/"})
                        uid = data.get("data", {}).get("userId")
                        if uid is not None:
                            cookies.append({"name": "userId", "value": str(uid),
                                            "domain": url.split("//")[1].split(":")[0],
                                            "path": "/"})
                            cookies.append({"name": "name", "value": args.account,
                                            "domain": url.split("//")[1].split(":")[0],
                                            "path": "/"})
                        if cookies:
                            await context.add_cookies(cookies)
                        print(f"  ✅ 已用 {args.account} 登录")
                    else:
                        print(f"  ⚠️ 登录失败: {data}")
            except Exception as e:
                print(f"  ⚠️ 预登录失败: {type(e).__name__}: {e}")

        def on_request(req):
            if req.resource_type in ("document", "xhr", "fetch"):
                try:
                    body = req.post_data or ""
                except Exception:
                    body = ""
                requests.append({
                    "method": req.method,
                    "url": req.url,
                    "headers": dict(req.headers),
                    "body": body,
                    "ts": time.time(),
                })
                print(f"  📡 {req.method} {req.url}")

        page.on("request", on_request)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  ⚠️ 打开页面失败: {type(e).__name__}: {e}")

        # 等待用户按 Enter 结束
        await asyncio.get_event_loop().run_in_executor(None, input, "\n操作完成后按 Enter 结束录制...")

        await browser.close()

    print(f"\n  共捕获 {len(requests)} 个请求")

    if not requests:
        print("  没有捕获到请求，结束")
        return

    # 上传到服务器
    print(f"  正在上传到服务器 {server} ...")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                server + "/api/record/import",
                json={"requests": requests})
            data = resp.json()
            if data.get("ok"):
                print(f"  ✅ 上传成功！共 {data.get('count', len(requests))} 个请求")
                print(f"  请到测试平台「录制」页面查看并导入")
            else:
                print(f"  ❌ 上传失败: {data.get('msg')}")
    except Exception as e:
        print(f"  ❌ 上传失败: {type(e).__name__}: {e}")
        # 保存到本地文件兜底
        fname = f"recorded_{int(time.time())}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(requests, f, ensure_ascii=False, indent=2)
        print(f"  已保存到本地文件: {fname}")


if __name__ == "__main__":
    asyncio.run(main())
