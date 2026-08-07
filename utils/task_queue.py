# -*- coding: utf-8 -*-
"""
Redis 分布式任务队列 — 万级任务派发 + 多进程/多机消费

Redis 角色 (隔离部署: zhilian-net 网络, 127.0.0.1:6379, 不碰公司服务):
  - 队列:      zhaopin:tasks:queue          (LIST, 待办 number)
  - 去重:      zhaopin:tasks:seen            (SET, 跨 producer 去重)
  - 已完成:    zhaopin:tasks:done            (SET, 统计)
  - 失败:      zhaopin:tasks:failed          (SET, 重试超限)
  - 尝试次数:  zhaopin:tasks:attempts:<num>  (计数)

Worker 多进程安全: Redis 原子操作 (SADD/RPUSH/BLPOP), 天然多消费者,
不同 worker 拿到不同任务。进程崩溃最多丢一个在途任务 (BLPOP 语义)。

数据本体仍入 SQLite (positions/companies), Redis 只做任务调度 + 全局协调。
"""
from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

PREFIX = "zhaopin:tasks:"
DEFAULT_URL = "redis://127.0.0.1:6379/0"


class TaskQueue:
    """Redis 异步任务队列。"""

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
        """阻塞取一个任务 (BLPOP, 多 worker 互不重复), 标记 processing。"""
        item = await self.redis.blpop(f"{PREFIX}queue", timeout=timeout)
        if not item:
            return None
        num = item[1]
        await self.redis.sadd(f"{PREFIX}processing", num)
        return num

    async def complete(self, number: str) -> None:
        await self.redis.srem(f"{PREFIX}processing", number)
        await self.redis.sadd(f"{PREFIX}done", number)

    async def fail(self, number: str, max_attempts: int = 3) -> bool:
        """失败: 尝试次数内重新入队, 否则标记 failed。返回是否重试。"""
        await self.redis.srem(f"{PREFIX}processing", number)
        n = await self.redis.incr(f"{PREFIX}attempts:{number}")
        if n <= max_attempts:
            await self.redis.rpush(f"{PREFIX}queue", number)
            logger.warning("任务 %s 第 %d 次失败, 重新入队", number, n)
            return True
        await self.redis.sadd(f"{PREFIX}failed", number)
        logger.warning("任务 %s 失败 %d 次, 标记 failed", number, n)
        return False

    async def is_drained(self) -> bool:
        """队列空且无处理中任务 (worker 退出判定, 避免误退丢在途任务)。"""
        return (await self.redis.llen(f"{PREFIX}queue")) == 0 and \
               (await self.redis.scard(f"{PREFIX}processing")) == 0

    # ---- stats / 运维 ----

    async def stats(self) -> dict:
        return {
            "queue": await self.redis.llen(f"{PREFIX}queue"),
            "processing": await self.redis.scard(f"{PREFIX}processing"),
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
