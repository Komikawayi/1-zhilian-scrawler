# -*- coding: utf-8 -*-
"""AsyncRateLimiter 令牌桶自检."""
from __future__ import annotations

import asyncio

from utils.async_client import AsyncRateLimiter


async def _elapsed(coro):
    t0 = asyncio.get_event_loop().time()
    await coro
    return asyncio.get_event_loop().time() - t0


def test_rate_zero_no_limit():
    async def _():
        rl = AsyncRateLimiter(0)
        await rl.acquire()   # 不限速, 立即返回
        return True
    assert asyncio.run(_())


def test_burst_capacity():
    """容量 = rate, 首轮可突发消耗 (不等待)。"""
    async def _():
        rl = AsyncRateLimiter(rate_per_sec=50)
        t = await _elapsed(asyncio.gather(*[rl.acquire() for _ in range(10)]))
        return t
    assert asyncio.run(_()) < 0.3   # 10 个在容量内, 突发完成


def test_rate_enforced():
    """rate=10/s, 25 次 acquire 需 ~1.5s (首轮容量突发 10, 后 15 个按 10/s 补)。"""
    async def _():
        rl = AsyncRateLimiter(rate_per_sec=10)
        t = await _elapsed(asyncio.gather(*[rl.acquire() for _ in range(25)]))
        return t
    t = asyncio.run(_())
    assert 1.2 < t < 2.5


def test_concurrent_workers_share_rate():
    """多个并发任务共享同一限速器 (跨 worker 全局限速)。"""
    async def _():
        rl = AsyncRateLimiter(rate_per_sec=20)
        async def worker():
            for _ in range(10):
                await rl.acquire()
        t = await _elapsed(asyncio.gather(*[worker() for _ in range(3)]))
        return t
    t = asyncio.run(_())
    # 3 worker × 10 = 30 次, 首轮突发 20, 后 10 个按 20/s -> ~0.5s
    assert 0.2 < t < 1.5
