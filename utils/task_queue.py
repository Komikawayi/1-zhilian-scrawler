# -*- coding: utf-8 -*-
"""
Redis 分布式任务队列 — 万级任务派发 + 多进程/多机消费 (崩溃安全版)

Redis 角色 (隔离部署: zhilian-net 网络, 127.0.0.1:6379, 不碰公司服务):
  - 队列:      zhaopin:tasks:queue          (LIST, 待办 number)
  - 处理中:    zhaopin:tasks:processing      (HASH: number→claimed_at, 崩溃回收)
  - 去重:      zhaopin:tasks:seen            (SET, 跨 producer 去重)
  - 已完成:    zhaopin:tasks:done            (SET)
  - 失败:      zhaopin:tasks:failed          (SET, 重试超限)
  - 尝试次数:  zhaopin:tasks:attempts        (HASH: number→count, 带 TTL 防泄漏)

崩溃安全 (redis-patterns):
  - processing 存 claimed_at 时间戳; recover_stale() 用分布式锁
    (SET NX PX) 回收超时未完成的任务重新入队 → worker 崩溃不丢任务、不卡死
  - attempts 用 HASH 而非散 key, complete/failed 清理 + EXPIRE 兜底 → 无内存泄漏

Worker 多进程安全: Redis 原子操作 (SADD/RPUSH/BLPOP/HSET), 天然多消费者。
数据本体仍入 PostgreSQL (positions/companies), Redis 只做任务调度 + 全局协调。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

PREFIX = "zhaopin:tasks:"
LOCK_PREFIX = "zhaopin:lock:"
DEFAULT_URL = "redis://127.0.0.1:6379/0"

# 默认: 在途任务超过该秒数视为 worker 崩溃, 回收重新入队
STALE_MAX_AGE = 120
# attempts hash 兜底 TTL (正常 complete/failed 会清理, 防异常累积)
ATTEMPTS_TTL = 86400


class TaskQueue:
    """Redis 异步任务队列 (崩溃安全)。"""

    def __init__(self, redis_url: str = DEFAULT_URL):
        self.redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    # ---- producer ----

    async def enqueue(self, number: str) -> bool:
        """入队; 返回是否新任务 (去重)。"""
        if await self.redis.sadd(f"{PREFIX}seen", number):
            await self.redis.rpush(f"{PREFIX}queue", number)
            return True
        return False

    async def enqueue_many(self, numbers) -> int:
        """批量入队 (去重), 返回新增数。"""
        added = 0
        for num in numbers:
            if await self.enqueue(num):
                added += 1
        return added

    # ---- consumer ----

    async def dequeue(self, timeout: int = 1) -> Optional[str]:
        """阻塞取一个任务 (BLPOP), 标记 processing (记录领取时间)。"""
        item = await self.redis.blpop(f"{PREFIX}queue", timeout=timeout)
        if not item:
            return None
        num = item[1]
        await self.redis.hset(f"{PREFIX}processing", num, str(time.time()))
        return num

    async def complete(self, number: str) -> None:
        await self.redis.hdel(f"{PREFIX}processing", number)
        await self.redis.sadd(f"{PREFIX}done", number)

    async def fail(self, number: str, max_attempts: int = 3) -> bool:
        """失败: 尝试次数内重新入队, 否则标记 failed。返回是否重试。"""
        await self.redis.hdel(f"{PREFIX}processing", number)
        n = await self.redis.hincrby(f"{PREFIX}attempts", number, 1)
        await self.redis.expire(f"{PREFIX}attempts", ATTEMPTS_TTL)   # TTL 兜底防泄漏
        if n <= max_attempts:
            await self.redis.rpush(f"{PREFIX}queue", number)
            logger.warning("任务 %s 第 %d 次失败, 重新入队", number, n)
            return True
        await self.redis.sadd(f"{PREFIX}failed", number)
        await self.redis.hdel(f"{PREFIX}attempts", number)           # 清理计数
        logger.warning("任务 %s 失败 %d 次, 标记 failed", number, n)
        return False

    async def recover_stale(self, max_age: int = STALE_MAX_AGE) -> int:
        """回收崩溃 worker 的在途任务 (分布式锁保证单实例执行)。

        扫描 processing, 领取时间超过 max_age 的任务重新入队。
        """
        lock = f"{LOCK_PREFIX}recover"
        if not await self.redis.set(lock, "1", nx=True, ex=30):
            return 0
        try:
            now = time.time()
            rec = await self.redis.hgetall(f"{PREFIX}processing")
            stale = [num for num, ts in rec.items()
                     if now - float(ts) > max_age]
            for num in stale:
                await self.redis.hdel(f"{PREFIX}processing", num)
                await self.redis.rpush(f"{PREFIX}queue", num)
            if stale:
                logger.warning("回收 %d 个崩溃在途任务, 重新入队", len(stale))
            return len(stale)
        finally:
            await self.redis.delete(lock)

    async def is_drained(self) -> bool:
        """队列空且无处理中任务 (worker 退出判定)。"""
        return (await self.redis.llen(f"{PREFIX}queue")) == 0 and \
               (await self.redis.hlen(f"{PREFIX}processing")) == 0

    # ---- stats / 运维 ----

    async def stats(self) -> dict:
        return {
            "queue": await self.redis.llen(f"{PREFIX}queue"),
            "processing": await self.redis.hlen(f"{PREFIX}processing"),
            "seen": await self.redis.scard(f"{PREFIX}seen"),
            "done": await self.redis.scard(f"{PREFIX}done"),
            "failed": await self.redis.scard(f"{PREFIX}failed"),
        }

    async def clear(self) -> None:
        """清空所有任务键 (重建任务池用)。"""
        keys = await self.redis.keys(f"{PREFIX}*")
        if keys:
            await self.redis.delete(*keys)
        logger.info("任务队列已清空 (%d 键)", len(keys))

    async def close(self) -> None:
        await self.redis.aclose()
