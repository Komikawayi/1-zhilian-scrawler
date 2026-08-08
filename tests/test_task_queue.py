# -*- coding: utf-8 -*-
"""Redis 任务队列自检 (需隔离 Redis 容器运行, 用 db=15 测试库, 不碰主队列 db=0)."""
from __future__ import annotations

import asyncio
import time

import pytest

from utils.task_queue import (
    PREFIX, TASK_COMPANY, TASK_KEYWORD, TASK_POSITION,
    QUEUE_SEARCH, QUEUE_POSITION, TaskQueue,
)

TEST_URL = "redis://127.0.0.1:6379/15"


@pytest.fixture(scope="session", autouse=True)
def redis_available():
    """Redis 不可用时跳过全部队列测试 (CI/无容器环境不崩)。"""
    async def _ping():
        q = TaskQueue(TEST_URL)
        try:
            await q.redis.ping()
        except Exception:
            pytest.skip("Redis 不可用 (需启动隔离的 zhilian-redis 容器)", allow_module_level=True)
        finally:
            await q.close()
    asyncio.run(_ping())


def _run(coro):
    return asyncio.run(coro)


def _clear():
    async def _():
        for qtype in (QUEUE_SEARCH, QUEUE_POSITION):
            q = TaskQueue(TEST_URL, queue_type=qtype)
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
            # max_attempts=2: 第1次失败重试, 第2次失败标记 failed
            for _ in range(2):
                num = await q.dequeue(timeout=1)
                assert num == "X"
                retry = await q.fail(num, max_attempts=2)
                if retry:
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


def test_recover_stale():
    """崩溃回收: 超时在途任务重新入队。"""
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            # 清理可能残留的 recover 锁 (前序测试 test_recover_lock_single 持锁 30s)
            await q.redis.delete("zhaopin:lock:recover:position")
            await q.enqueue("A")
            await q.dequeue(timeout=1)                        # A 进 processing
            old = str(time.time() - 200)
            await q.redis.hset(f"{PREFIX}position:processing", "A", old)  # 伪造超时
            n = await q.recover_stale(max_age=120)
            assert n == 1
            s = await q.stats()
            assert s["processing"] == 0 and s["queue"] == 1   # A 重新入队
        finally:
            await q.close()
    _run(_())


def test_recover_lock_single():
    """recover 分布式锁: 第二个调用不重复执行。"""
    _clear()
    async def _():
        q = TaskQueue(TEST_URL)
        try:
            await q.redis.set("zhaopin:lock:recover:position", "1", ex=30)  # 模拟他人持锁
            n = await q.recover_stale()
            assert n == 0
            # 释放锁, 避免残留污染后续测试 (锁 ex=30s > 测试时长)
            await q.redis.delete("zhaopin:lock:recover:position")
        finally:
            await q.close()
    _run(_())


# ====================================================================
# 双队列 (搜索/详情) + 任务类型前缀
# ====================================================================

def test_task_id_factories():
    """任务 id 构造: 类型前缀 + 跨队列不撞。"""
    assert TaskQueue.make_keyword_task("653", "smt") == f"{TASK_KEYWORD}:653:smt"
    assert TaskQueue.make_company_task("CZ883210900") == f"{TASK_COMPANY}:CZ883210900"
    assert TaskQueue.make_position_task("CC883210900J40880383610") == \
        f"{TASK_POSITION}:CC883210900J40880383610"
    assert TaskQueue.task_type(TASK_KEYWORD + ":653:smt") == TASK_KEYWORD
    assert TaskQueue.task_type(TASK_COMPANY + ":CZ883210900") == TASK_COMPANY
    assert TaskQueue.task_type(TASK_POSITION + ":CCLxxx") == TASK_POSITION


def test_invalid_queue_type():
    """非法队列类型直接抛错 (防拼错键名)。"""
    try:
        TaskQueue(TEST_URL, queue_type="bogus")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_dual_queue_isolated():
    """搜索/详情双队列键隔离: 入队互不干扰, stats 各自独立。"""
    _clear()
    async def _():
        sq = TaskQueue(TEST_URL, queue_type=QUEUE_SEARCH)
        pq = TaskQueue(TEST_URL, queue_type=QUEUE_POSITION)
        try:
            kw_task = sq.make_keyword_task("653", "smt")
            pos_task = pq.make_position_task("CCLAAA")
            assert await sq.enqueue(kw_task) is True
            assert await pq.enqueue(pos_task) is True
            # 同 id 跨队列也可各自存在 (前缀不同, 键不同)
            assert await sq.enqueue(pos_task) is True
            ss, ps = await sq.stats(), await pq.stats()
            assert ss["queue"] == 2 and ps["queue"] == 1
            assert await sq.dequeue(timeout=1) == kw_task
            assert ss["queue"] == 2  # stats 快照不影响
            await sq.close()
            await pq.close()
            s2 = await TaskQueue(TEST_URL, queue_type=QUEUE_SEARCH).stats()
            p2 = await TaskQueue(TEST_URL, queue_type=QUEUE_POSITION).stats()
            assert s2["processing"] == 1 and p2["processing"] == 0
        finally:
            await sq.close()
            await pq.close()
    _run(_())


def test_dual_queue_drained_independent():
    """is_drained 按队列独立: 详情空不代表搜索空。"""
    _clear()
    async def _():
        sq = TaskQueue(TEST_URL, queue_type=QUEUE_SEARCH)
        pq = TaskQueue(TEST_URL, queue_type=QUEUE_POSITION)
        try:
            assert await sq.is_drained() and await pq.is_drained()
            await sq.enqueue(sq.make_keyword_task("530", "pcba"))
            assert not await sq.is_drained()
            assert await pq.is_drained()      # 详情队列仍空
        finally:
            await sq.close()
            await pq.close()
    _run(_())


def test_clear_scope():
    """clear 只清本队列类型: 搜索清空不影响详情。"""
    _clear()
    async def _():
        sq = TaskQueue(TEST_URL, queue_type=QUEUE_SEARCH)
        pq = TaskQueue(TEST_URL, queue_type=QUEUE_POSITION)
        try:
            await sq.enqueue(sq.make_keyword_task("653", "smt"))
            await pq.enqueue(pq.make_position_task("CCLBBB"))
            await sq.clear()
            ss, ps = await sq.stats(), await pq.stats()
            assert ss["queue"] == 0 and ps["queue"] == 1
        finally:
            await sq.close()
            await pq.close()
    _run(_())
