# -*- coding: utf-8 -*-
"""Redis 任务队列自检 (需隔离 Redis 容器运行, 用 db=15 测试库, 不碰主队列 db=0)."""
from __future__ import annotations

import asyncio

from utils.task_queue import TaskQueue

TEST_URL = "redis://127.0.0.1:6379/15"


def _run(coro):
    return asyncio.run(coro)


def _clear():
    async def _():
        q = TaskQueue(TEST_URL)
        await q.clear()
        await q.close()
    _run(_())


def test_enqueue_dedup():
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            assert await q.enqueue("N1") is True
            assert await q.enqueue("N1") is False   # 去重
            assert await q.enqueue("N2") is True
            s = await q.stats()
            assert s["queue"] == 2 and s["seen"] == 2
        finally:
            await q.close()
    _run(_())


def test_dequeue_complete():
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            await q.enqueue("A")
            await q.enqueue("B")
            first = await q.dequeue(timeout=1)
            assert first in ("A", "B")
            s = await q.stats()
            assert s["processing"] == 1          # 已领未完成
            assert not await q.is_drained()       # 还有在途
            await q.complete(first)
            s2 = await q.stats()
            assert s2["done"] == 1 and s2["processing"] == 0
        finally:
            await q.close()
    _run(_())


def test_fail_retry_then_failed():
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            await q.enqueue("X")
            for _ in range(3):
                num = await q.dequeue(timeout=1)
                assert num == "X"
                retry = await q.fail(num, max_attempts=2)
                if num == "X" and retry:
                    continue
            s = await q.stats()
            assert s["failed"] == 1              # 超限标记失败
            assert s["queue"] == 0
        finally:
            await q.close()
    _run(_())


def test_is_drained():
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            assert await q.is_drained()           # 空队列 = drained
            await q.enqueue("Z")
            assert not await q.is_drained()
            await q.dequeue(timeout=1)
            assert not await q.is_drained()       # 在途
            await q.complete("Z")
            assert await q.is_drained()
        finally:
            await q.close()
    _run(_())
