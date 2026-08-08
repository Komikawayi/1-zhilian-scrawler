# -*- coding: utf-8 -*-
"""搜索接口最大吞吐压测: 30s 无限制速, 多协程并发打 sou.zhaopin.com。"""
import asyncio, sys, time, random
sys.path.insert(0, '.')
from curl_cffi import requests as cffi

KWS = ['smt', 'pcba', '贴片', '回流焊', 'AOI', '锡膏', 'SMT工程师', '电子制造']
CITY = '653'  # 杭州

def classify(text):
    if 'Security Verification' in text: return 'CAPTCHA'
    if 'solveChallenge' in text: return 'CHALLENGE'
    if '__INITIAL_STATE__' in text: return 'OK'
    return 'UNKNOWN'

async def worker(session, n, results, stop, lock, t0, duration):
    ok = 0
    while time.time() - t0 < duration and not stop[0]:
        kw = random.choice(KWS)
        p = random.randint(1, 30)  # 随机页码, 避免同一页缓存
        url = f'https://sou.zhaopin.com/?jl={CITY}&kw={kw}&p={p}'
        try:
            r = await session.get(url, timeout=20, allow_redirects=True)
            cls = classify(r.text)
            async with lock:
                results[cls] = results.get(cls, 0) + 1
            if cls == 'CAPTCHA':
                stop[0] = True
                print(f'worker{n} 触发 CAPTCHA, 停止')
                break
            ok += 1
        except Exception:
            async with lock:
                results['ERR'] = results.get('ERR', 0) + 1
    return ok

async def main():
    DURATION = 30.0
    CONCURRENCY = 10
    t0 = time.time()
    session = cffi.AsyncSession(impersonate='chrome')
    results = {}
    stop = [False]
    lock = asyncio.Lock()
    tasks = [asyncio.create_task(worker(session, i, results, stop, lock, t0, DURATION))
             for i in range(CONCURRENCY)]
    await asyncio.gather(*tasks)
    await session.close()
    el = time.time() - t0
    total = sum(results.values())
    print(f'\n=== 结果 ({el:.1f}s) ===')
    print(f'总请求: {total}  实际速率: {total/el:.1f} req/s')
    for k, v in sorted(results.items()):
        print(f'  {k}: {v}')

asyncio.run(main())
