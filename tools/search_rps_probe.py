# -*- coding: utf-8 -*-
"""
搜索接口最大 RPS 探针 (独立脚本, 不影响运行中的 consume)

用法:
  py tools/search_rps_probe.py --city 904 --kw SMT
  py tools/search_rps_probe.py --concs "1,2,5,10,20,30,50" --each 5

设计:
  - 用独立的 curl_cffi AsyncSession (新 TLS 指纹/连接, 不与运行中 run.py 共享)
  - 逐档递增并发, 每档持续 each 秒, 测实际成功 RPS + 延迟分位
  - 每档间停顿, 让服务端计数回落, 避免上一档影响下一档
  - 检测风控特征: Security Verification (验证码) / 缺 __INITIAL_STATE__ (challenge)
  - 结果: 每档成功数/失败数/RPS/TTFB 中位, 及风控标记, 帮你找单 IP 搜索软限

注意: 探针会在当前 IP 上叠加请求, 与 run.py 共享风控面。默认每档并发
较小且短暂, 对运行中任务影响可控; 若发现风控标记立即停止并降并发。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi.const import CurlInfo

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rps_probe")

# 搜索页 (SSR): 与 collect.py _search_page_async 同 URL 构造
_SEARCH_URL = "https://sou.zhaopin.com/?jl={city}&kw={q}&p={p}"


async def _fetch(session, city: str, kw: str, page: int) -> tuple[int, float, str]:
    """发一个搜索请求, 返回 (状态码, TTFB, 页面特征)。"""
    url = _SEARCH_URL.format(city=city, q=quote(kw), p=page)
    t0 = time.perf_counter()
    resp = await session.get(url, timeout=15, allow_redirects=True)
    ttfb = time.perf_counter() - t0
    text = resp.text
    if "Security Verification" in text:
        feature = "CAPTCHA"
    elif "__INITIAL_STATE__" not in text:
        feature = "CHALLENGE"
    else:
        feature = "OK"
    return resp.status_code, ttfb, feature


async def _run_stage(concurrency: int, duration: float, city: str, kw: str,
                     page: int, max_clients: int = 0) -> dict:
    """跑一档: 固定并发, 持续 duration 秒, 统计成功/失败/RPS/延迟/风控。"""
    pool = max_clients or concurrency
    session = cffi_requests.AsyncSession(impersonate="chrome", max_clients=pool,
                                         curl_infos=[CurlInfo.STARTTRANSFER_TIME,
                                                     CurlInfo.TOTAL_TIME])
    ok = fail = captcha = challenge = 0
    lat: list[float] = []
    start = time.perf_counter()
    end = start + duration
    stop = asyncio.Event()
    async def _worker():
        nonlocal ok, fail, captcha, challenge
        while time.perf_counter() < end and not stop.is_set():
            try:
                code, ttfb, feat = await _fetch(session, city, kw, page)
                if code == 200 and feat == "OK":
                    ok += 1
                    lat.append(ttfb)
                elif feat == "CAPTCHA":
                    captcha += 1
                    fail += 1
                    stop.set()
                elif feat == "CHALLENGE":
                    challenge += 1
                    fail += 1
                    stop.set()
                else:
                    fail += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                if fail <= 3:
                    print(f"  [err] {type(e).__name__}: {str(e)[:140]}", flush=True)
            # 每请求后固定节流: 请求本身已串行, 间隔 = 1/期望速率;
            # 用 5ms 兜底防忙循环 (异常路径不阻塞时也不打爆)
            await asyncio.sleep(0.005)
    tasks = [asyncio.create_task(_worker()) for _ in range(concurrency)]
    await asyncio.gather(*tasks)
    await session.close()
    dur = time.perf_counter() - start
    lat.sort()
    n = len(lat)
    return {
        "concurrency": concurrency,
        "max_clients": pool,
        "rps": ok / dur,
        "ok": ok, "fail": fail,
        "captcha": captcha, "challenge": challenge,
        "lat50": lat[n // 2] * 1000 if n else 0,
        "lat95": lat[int(n * 0.95)] * 1000 if n else 0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="904")
    ap.add_argument("--kw", default="SMT")
    ap.add_argument("--concs", default="1,2,5,10,20,30,50")
    ap.add_argument("--each", type=float, default=5.0, help="每档持续时间(秒)")
    ap.add_argument("--max-clients", type=int, default=0,
                    help="每档连接池上限, 默认等于并发")
    ap.add_argument("--page", type=int, default=1)
    args = ap.parse_args()
    concs = [int(x) for x in args.concs.split(",")]
    print(f"探针: city={args.city} kw={args.kw} page={args.page} "
          f"并发档位={concs} 每档{args.each}s "
          f"连接池={args.max_clients or '跟随并发'}")
    print("注意: 探针叠加在运行中 consume 之上, 发现风控标记即停.")
    print(f"{'并发':>4} {'池':>4} {'成功':>6} {'失败':>5} {'RPS':>6} {'LAT50ms':>8} "
          f"{'LAT95ms':>8} {'风控标记':>10}")
    for c in concs:
        r = await _run_stage(c, args.each, args.city, args.kw, args.page,
                             args.max_clients)
        flag = []
        if r["captcha"]:
            flag.append(f"验证码{r['captcha']}")
        if r["challenge"]:
            flag.append(f"challenge{r['challenge']}")
        print(f"{r['concurrency']:>4} {r['max_clients']:>4} {r['ok']:>6} {r['fail']:>5} "
              f"{r['rps']:>6.1f} {r['lat50']:>8.1f} {r['lat95']:>8.1f} "
              f"{'/'.join(flag) if flag else '-':>10}")
        # 风控触发则提前终止, 避免打爆 IP
        if r["captcha"] > 0 or r["challenge"] > 0:
            print("⚠ 检测到风控特征, 停止探测 (并发过高).")
            return
        await asyncio.sleep(2.0)   # 档间停顿, 服务端计数回落


if __name__ == "__main__":
    asyncio.run(main())
