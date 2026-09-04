#!/usr/bin/env python3
"""
只压测「图像帧」接口：/xa_brain_encrypt/XA-00001/{studyUID}/{seriesUID}/{sopInstanceUID}

其他接口（登录、studies、dcp、缩略图）全部去掉。

准备阶段（不计入压测）：
  - 从 /api/repacs/series/{seriesUID}/dcp 一次性获取全部帧的 storagePath
压测阶段：
  - 并发请求这些帧 URL，统计 QPS、成功率、耗时分布

用法：
    .venv/bin/python stress_frame.py --concurrency 50 --duration 30
    .venv/bin/python stress_frame.py --concurrency 100 --requests 5000
    .venv/bin/python stress_frame.py --concurrency 50 --duration 60 --loop   # 循环取帧
"""

import argparse
import asyncio
import json
import statistics
import time

import httpx

BASE_URL = "http://192.168.108.109:8000"
STUDY_UID = "1.3.46.670589.28.68172260368172520200518000449629596"
SERIES_UID = "1.3.46.670589.28.681722603681725.20200518010931497060.2.2"


def fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


def pct(vals, p):
    if not vals:
        return 0.0
    return statistics.quantiles(vals, n=100, method="inclusive")[p - 1]


async def fetch_frames(base: str) -> list[str]:
    """准备阶段：从 dcp 接口获取全部帧的 storagePath，拼成完整 URL。"""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{base}/api/repacs/series/{SERIES_UID}/dcp")
        data = resp.json()
        images = data.get("images", [])
        return [f"{base}/{img['storagePath']}" for img in images]


async def worker(urls: list[str], loop: bool, stop_at: float,
                 results: list, lock: asyncio.Lock) -> None:
    """单个并发 worker：循环取帧请求，直到到达停止时间。"""
    n = len(urls)
    i = 0
    async with httpx.AsyncClient(timeout=120) as client:
        while time.perf_counter() < stop_at:
            url = urls[i % n]
            i += 1
            if not loop and i > n:
                break
            t0 = time.perf_counter()
            try:
                resp = await client.get(url)
                ms = (time.perf_counter() - t0) * 1000
                ok = resp.status_code == 200
                size = len(resp.content) if ok else 0
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                ok = False
                size = 0
            async with lock:
                results.append({"ok": ok, "ms": ms, "size": size})


async def main() -> None:
    parser = argparse.ArgumentParser(description="只压测图像帧接口")
    parser.add_argument("--concurrency", type=int, default=50, help="并发数")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="压测持续时间(秒)，与 --requests 二选一")
    parser.add_argument("--requests", type=int, default=0,
                        help="总请求数(0 表示按 duration 计时)")
    parser.add_argument("--loop", action="store_true",
                        help="循环取帧(默认每帧只请求一次)")
    parser.add_argument("--base-url", default=BASE_URL, help="产品地址")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")

    print("=" * 72)
    print("图像帧接口压测  /xa_brain_encrypt/XA-00001/...")
    print(f"目标: {base}")
    print(f"并发: {args.concurrency}   "
          f"{'持续 ' + str(args.duration) + 's' if args.requests == 0 else '总请求 ' + str(args.requests)}"
          f"  循环取帧: {'是' if args.loop else '否'}")
    print("=" * 72)

    # ---- 准备：获取帧列表 ----
    print("\n[准备] 获取帧列表 (dcp) ...")
    t0 = time.perf_counter()
    urls = await fetch_frames(base)
    print(f"  ✅ 共 {len(urls)} 帧  {fmt_ms((time.perf_counter() - t0) * 1000)}")
    if not urls:
        print("未获取到帧，终止。")
        return

    # ---- 压测 ----
    results: list[dict] = []
    lock = asyncio.Lock()

    if args.requests > 0:
        # 按总请求数：每个 worker 分到固定数量
        per = args.requests // args.concurrency
        extra = args.requests % args.concurrency
        stop_at = time.perf_counter() + 3600  # 足够长
        tasks = []
        for w in range(args.concurrency):
            cnt = per + (1 if w < extra else 0)
            # 用子列表限制每个 worker 的请求数
            async def run_worker(cnt=cnt):
                n = len(urls)
                i = 0
                done = 0
                async with httpx.AsyncClient(timeout=120) as client:
                    while done < cnt:
                        url = urls[i % n]
                        i += 1
                        done += 1
                        t0 = time.perf_counter()
                        try:
                            resp = await client.get(url)
                            ms = (time.perf_counter() - t0) * 1000
                            ok = resp.status_code == 200
                            size = len(resp.content) if ok else 0
                        except Exception:
                            ms = (time.perf_counter() - t0) * 1000
                            ok = False
                            size = 0
                        async with lock:
                            results.append({"ok": ok, "ms": ms, "size": size})
            tasks.append(asyncio.create_task(run_worker()))
        t_start = time.perf_counter()
        await asyncio.gather(*tasks)
        total_ms = (time.perf_counter() - t_start) * 1000
    else:
        stop_at = time.perf_counter() + args.duration
        tasks = [asyncio.create_task(
            worker(urls, args.loop, stop_at, results, lock))
            for _ in range(args.concurrency)]
        t_start = time.perf_counter()
        await asyncio.gather(*tasks)
        total_ms = (time.perf_counter() - t_start) * 1000

    # ---- 汇总 ----
    total = len(results)
    ok = [r for r in results if r["ok"]]
    fail = total - len(ok)
    times = [r["ms"] for r in ok]
    sizes = [r["size"] for r in ok]
    qps = total / (total_ms / 1000) if total_ms > 0 else 0
    total_mb = sum(sizes) / 1024 / 1024

    print("\n" + "=" * 72)
    print("压测结果汇总")
    print("=" * 72)
    print(f"总请求数        : {total}")
    print(f"成功            : {len(ok)}")
    print(f"失败            : {fail}")
    print(f"成功率          : {len(ok) / total * 100:.2f}%" if total else "成功率: -")
    print(f"总耗时          : {total_ms / 1000:.1f}s")
    print(f"QPS             : {qps:.1f} 请求/秒")
    print(f"吞吐量          : {total_mb / (total_ms / 1000):.1f} MB/s  (共 {total_mb:.1f} MB)")
    if times:
        print(f"\n[响应耗时 ms]   avg={statistics.mean(times):.0f}  "
              f"min={min(times):.0f}  max={max(times):.0f}  "
              f"p50={pct(times, 50):.0f}  p95={pct(times, 95):.0f}  "
              f"p99={pct(times, 99):.0f}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
