# -*- coding: utf-8 -*-
"""
详情接口 (position-detailv2) 最大 RPS 探针 (独立脚本, 不影响运行中 consume)

用法:
  py tools/detail_rps_probe.py --concs "1,5,10,20,40,80,120"
  py tools/detail_rps_probe.py --number CC1234567890J... --concs "10,20"

设计:
  - 从 Redis position:queue 取几个真实岗位号 (只 lrange 读, 不消费不中断任务)
  - 独立 curl_cffi AsyncSession + fe-api 请求 (与运行中 run.py 共享 IP 风控面)
  - 逐档递增并发测详情接口真实最大吞吐 + 延迟分位 + 风控/业务错误标记
  - 检测: 211(岗位失效) / 风控 / HTTP错误

注意: 叠加在运行中 run.py (详情 80/s) 之上, 若触发风控立即停止。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid

from curl_cffi import requests as cffi_requests
from curl_cffi.const import CurlInfo

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("detail_rps_probe")

DETAIL_V2_URL = "https://fe-api.zhaopin.com/c/i/jobs/position-detailv2"
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-zp-business-system": "1",
    "x-zp-page-code": "4019",
    "x-zp-platform": "13",
    "referer": "https://www.zhaopin.com/",
}


def _params(number: str) -> dict:
    return {
        "_v": "%.8f" % (time.time() % 1),
        "x-zp-page-request-id": uuid.uuid4().hex + "-" + str(int(time.time() * 1000)),
        "x-zp-client-id": str(uuid.uuid4()),
        "platform": "13",
        "version": "0.0.0",
        "number": number,
    }


async def _get_real_numbers(count: int) -> list:
    """从 Redis 队列取 count 个真实岗位号 (只读, 不消费)。"""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
        items = await r.lrange("zhaopin:tasks:position:queue", 0, count * 2 - 1)
        await r.aclose()
        nums = [x.split(":", 1)[1] for x in items if x.startswith("position:")]
        return nums[:count]
    except Exception as e:  # noqa: BLE001
        print(f"从 Redis 取岗位号失败: {e}, 用内置测试号")
        return ["CC541117820J40384299213"] * count


async def _fetch(session, number: str) -> tuple[int, float, str]:
    """发一个详情请求, 返回 (状态码, 耗时, 特征)。"""
    t0 = time.perf_counter()
    resp = await session.get(DETAIL_V2_URL, params=_params(number),
                             headers=_HEADERS, timeout=15)
    dt = time.perf_counter() - t0
    try:
        body = resp.json()
        code = body.get("code")
        api = body.get("apiCode")
        if code == 200 and api == 200:
            return resp.status_code, dt, "OK"
        if api == 211:
            return resp.status_code, dt, "211"
        return resp.status_code, dt, f"ERR{api}"
    except Exception:  # noqa: BLE001
        return resp.status_code, dt, "NONJSON"


async def _run_stage(concurrency: int, duration: float, numbers: list) -> dict:
    session = cffi_requests.AsyncSession(impersonate="chrome",
                                         curl_infos=[CurlInfo.STARTTRANSFER_TIME,
                                                     CurlInfo.TOTAL_TIME])
    ok = fail = died = err = 0
    lat: list[float] = []
    start = time.perf_counter()
    end = start + duration
    idx = 0

    async def _worker():
        nonlocal ok, fail, died, err, idx
        while time.perf_counter() < end:
            num = numbers[idx % len(numbers)]
            idx += 1
            try:
                sc, dt, feat = await _fetch(session, num)
                if feat == "OK":
                    ok += 1
                    lat.append(dt)
                elif feat == "211":
                    died += 1
                    fail += 1
                elif feat == "NONJSON":
                    err += 1
                    fail += 1
                else:
                    err += 1
                    fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            await asyncio.sleep(0.005)

    tasks = [asyncio.create_task(_worker()) for _ in range(concurrency)]
    await asyncio.gather(*tasks)
    await session.close()
    dur = time.perf_counter() - start
    lat.sort()
    n = len(lat)
    return {
        "concurrency": concurrency,
        "rps": ok / dur,
        "ok": ok, "fail": fail, "died": died, "err": err,
        "lat50": lat[n // 2] * 1000 if n else 0,
        "lat95": lat[int(n * 0.95)] * 1000 if n else 0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concs", default="1,5,10,20,40,80,120")
    ap.add_argument("--each", type=float, default=4.0)
    ap.add_argument("--numbers", default="", help="逗号分隔岗位号, 默认从 Redis 取")
    args = ap.parse_args()
    concs = [int(x) for x in args.concs.split(",")]
    if args.numbers:
        numbers = args.numbers.split(",")
    else:
        numbers = await _get_real_numbers(3)
    print(f"探针: 岗位号={numbers} 并发档位={concs} 每档{args.each}s")
    print("注意: 叠加在运行中 consume (详情80/s) 之上, 触发风控即停.")
    print(f"{'并发':>4} {'成功':>6} {'失败':>5} {'失效211':>6} {'RPS':>6} "
          f"{'TTFB50ms':>8} {'TTFB95ms':>8} {'标记':>8}")
    for c in concs:
        r = await _run_stage(c, args.each, numbers)
        flag = "验证码" if r["err"] > 0 else "-"
        print(f"{r['concurrency']:>4} {r['ok']:>6} {r['fail']:>5} {r['died']:>6} "
              f"{r['rps']:>6.1f} {r['lat50']:>8.1f} {r['lat95']:>8.1f} {flag:>8}")
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
