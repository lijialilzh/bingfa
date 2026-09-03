#!/usr/bin/env python3
"""
单独测试「下载图像」链路的所有接口。

链路（按实际调用顺序）：
  1. 登录            POST /api/v1/user/login
  2. 图像元数据      GET  /api/v1/studies?studyInstanceUID=...&taskType=xa_brain&product=XA_BRAIN
  3. 序列数据        GET  /api/repacs/series/{seriesInstanceUID}/dcp
  4. 缩略图          GET  /RESULT/thumbnail/{seriesInstanceUID}/thumbnail.jpg
  5. 图像帧          GET  /xa_brain_encrypt/XA-00001/{studyUID}/{seriesUID}/{sopInstanceUID}

用法：
    .venv/bin/python test_download_apis.py
    .venv/bin/python test_download_apis.py --frames 5      # 只下载前 5 帧
    .venv/bin/python test_download_apis.py --frames all    # 下载全部帧
"""

import argparse
import hashlib
import json
import sys
import time

import httpx

BASE_URL = "http://192.168.108.109:8000"
ACCOUNT = "test"
PASSWORD = "123qwe"
STUDY_UID = "1.3.46.670589.28.68172260368172520200518000449629596"
PRODUCT = "XA_BRAIN"
TASK_TYPE = "xa_brain"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def main() -> None:
    parser = argparse.ArgumentParser(description="单独测试下载图像的所有接口")
    parser.add_argument("--frames", default="3",
                        help="下载帧数：数字(前N帧) 或 all(全部帧)，默认 3")
    parser.add_argument("--base-url", default=BASE_URL, help="产品地址")
    parser.add_argument("--account", default=ACCOUNT, help="账号")
    parser.add_argument("--password", default=PASSWORD, help="密码")
    parser.add_argument("--study-uid", default=STUDY_UID, help="studyInstanceUID")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=120, follow_redirects=True)
    results = []

    def record(name: str, ok: bool, ms: float, detail: str) -> None:
        results.append({"接口": name, "结果": "✅ 通过" if ok else "❌ 失败",
                        "耗时": fmt_ms(ms), "详情": detail})
        print(f"  {'✅' if ok else '❌'} {name:<12} {fmt_ms(ms):>12}  {detail}")

    print("=" * 72)
    print(f"下载图像接口单独测试")
    print(f"目标: {base}   账号: {args.account}")
    print("=" * 72)

    # ---- 1. 登录 ----
    print("\n[1/5] 登录")
    t0 = time.perf_counter()
    try:
        resp = client.post(f"{base}/api/v1/user/login",
                           json={"name": args.account,
                                 "password": sha256(args.password)})
        data = resp.json()
        ok = resp.status_code == 200 and data.get("code") == 10000
        token = resp.cookies.get("token", "")
        uid = data.get("data", {}).get("userId")
        record("登录", ok, (time.perf_counter() - t0) * 1000,
               f"userId={uid} token={token[:8]}…" if ok else resp.text[:120])
        if not ok:
            print("登录失败，终止。")
            sys.exit(1)
    except Exception as e:
        record("登录", False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
        sys.exit(1)

    # ---- 2. 图像元数据 ----
    print("\n[2/5] 图像元数据 (studies)")
    t0 = time.perf_counter()
    try:
        resp = client.get(f"{base}/api/v1/studies",
                          params={"studyInstanceUID": args.study_uid,
                                  "taskType": TASK_TYPE, "product": PRODUCT})
        data = resp.json()
        studies = data.get("data") or []
        series_list = []
        total_frames = 0
        for study in studies:
            for series in study.get("series", []):
                series_list.append(series)
                total_frames += int(series.get("imgFrameNumber", 0))
        ok = resp.status_code == 200 and data.get("code") == 10000 and series_list
        record("图像元数据", ok, (time.perf_counter() - t0) * 1000,
               f"{len(series_list)} 个序列, 共 {total_frames} 帧")
        if not ok:
            print("未获取到序列，终止。")
            sys.exit(1)
    except Exception as e:
        record("图像元数据", False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
        sys.exit(1)

    series_uid = series_list[0]["seriesInstanceUID"]

    # ---- 3. 序列数据 (dcp) ----
    print("\n[3/5] 序列数据 (dcp)")
    t0 = time.perf_counter()
    try:
        resp = client.get(f"{base}/api/repacs/series/{series_uid}/dcp")
        data = resp.json()
        images = data.get("images", [])
        ok = resp.status_code == 200 and images
        record("序列数据", ok, (time.perf_counter() - t0) * 1000,
               f"{len(images)} 张图像, {fmt_size(len(resp.content))}")
        if not ok:
            print("未获取到图像列表，终止。")
            sys.exit(1)
    except Exception as e:
        record("序列数据", False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
        sys.exit(1)

    # ---- 4. 缩略图 ----
    print("\n[4/5] 缩略图 (thumbnail)")
    t0 = time.perf_counter()
    try:
        resp = client.get(f"{base}/RESULT/thumbnail/{series_uid}/thumbnail.jpg")
        ok = resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image")
        record("缩略图", ok, (time.perf_counter() - t0) * 1000,
               f"{fmt_size(len(resp.content))} {resp.headers.get('content-type', '')}")
    except Exception as e:
        record("缩略图", False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")

    # ---- 5. 图像帧 ----
    print("\n[5/5] 图像帧 (xa_brain_encrypt)")
    if args.frames == "all":
        targets = images
    else:
        targets = images[: int(args.frames)]
    print(f"  将下载 {len(targets)} 帧（共 {len(images)} 帧）")

    frame_times = []
    frame_sizes = []
    fail = 0
    for i, img in enumerate(targets, 1):
        path = img["storagePath"]
        url = f"{base}/{path}"
        t0 = time.perf_counter()
        try:
            resp = client.get(url)
            ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                frame_times.append(ms)
                frame_sizes.append(len(resp.content))
                print(f"    帧 {i:>3}/{len(targets)}  ✅ {fmt_ms(ms):>12}  "
                      f"{fmt_size(len(resp.content))}")
            else:
                fail += 1
                print(f"    帧 {i:>3}/{len(targets)}  ❌ HTTP {resp.status_code}")
        except Exception as e:
            fail += 1
            print(f"    帧 {i:>3}/{len(targets)}  ❌ {type(e).__name__}: {e}")

    if frame_times:
        avg = sum(frame_times) / len(frame_times)
        total_size = sum(frame_sizes)
        record("图像帧", fail == 0, avg,
               f"{len(frame_times)} 帧成功, {fail} 帧失败, "
               f"avg={fmt_ms(avg)}, min={fmt_ms(min(frame_times))}, "
               f"max={fmt_ms(max(frame_times))}, 总大小 {fmt_size(total_size)}")
    else:
        record("图像帧", False, 0, f"全部失败 ({fail} 帧)")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("测试结果汇总")
    print("=" * 72)
    for r in results:
        print(f"  {r['结果']}  {r['接口']:<12} {r['耗时']:>12}  {r['详情']}")
    passed = sum(1 for r in results if r["结果"].startswith("✅"))
    print(f"\n通过 {passed}/{len(results)} 个接口")
    print("=" * 72)


if __name__ == "__main__":
    main()
