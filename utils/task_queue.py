# -*- coding: utf-8 -*-
"""
Redis 分布式任务队列 — 搜索/详情双队列 (多进程分布式, 崩溃安全)

任务类型 (队列隔离, 各持独立键):
  search 队列    zhaopin:tasks:search:*     (搜索任务, 与详情共享全局限速)
    keyword:{city}:{kw}:{page}   关键词搜索任务 (阶段一, producer 生成)
    company:{company_number}     公司名搜索任务 (阶段二, 查缺补漏)
  position 队列  zhaopin:tasks:position:*   (岗位详情任务)
    position:{number}            岗位详情任务 (搜索消费后投递)

为什么分队列: 搜索任务消费后**产出新任务** (岗位号/公司号), 详情任务消费后
**入库** — 消费逻辑不同, 独立 worker 池可独立调并发; 但限速共享同一令牌桶
(同一 IP 信誉资源, RedisRateLimiter 全局 key 天然共享)。

Redis 键 (每个队列类型 type ∈ {search, position}):
  zhaopin:tasks:{type}:queue       (LIST, 待办)
  zhaopin:tasks:{type}:processing  (HASH: task_id→claimed_at, 崩溃回收)
  zhaopin:tasks:{type}:seen        (SET, 跨 producer 去重)
  zhaopin:tasks:{type}:done        (SET)
  zhaopin:tasks:{type}:failed      (SET, 重试超限)
  zhaopin:tasks:{type}:attempts    (HASH: task_id→count, 带 TTL 防泄漏)

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
from typing import Iterable, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

PREFIX = "zhaopin:tasks:"
LOCK_PREFIX = "zhaopin:lock:"
DEFAULT_URL = "redis://127.0.0.1:6379/0"

# 队列类型
QUEUE_SEARCH = "search"
QUEUE_POSITION = "position"
QUEUE_TYPES = (QUEUE_SEARCH, QUEUE_POSITION)

# 任务 id 前缀 (任务类型, 跨队列不撞)
TASK_KEYWORD = "keyword"     # keyword:{city}:{kw}:{page}
TASK_COMPANY = "company"     # company:{company_number}
TASK_POSITION = "position"   # position:{number}

# 默认: 在途任务超过该秒数视为 worker 崩溃, 回收重新入队
STALE_MAX_AGE = 120
# attempts hash 兜底 TTL (正常 complete/failed 会清理, 防异常累积)
ATTEMPTS_TTL = 86400


class TaskQueue:
    """Redis 异步任务队列 (单类型, 崩溃安全)。

    用法:
        q = TaskQueue(url, queue_type="position")
        await q.enqueue("position:CCLxxx")
        await q.dequeue()
    """

    def __init__(self, redis_url: str = DEFAULT_URL,
                 queue_type: str = QUEUE_POSITION) -> None:
        if queue_type not in QUEUE_TYPES:
            raise ValueError(f"queue_type 必须 ∈ {QUEUE_TYPES}, got {queue_type!r}")
        self.type = queue_type
        self.redis = aioredis.Redis.from_url(redis_url, decode_responses=True)
        self._queue = f"{PREFIX}{queue_type}:queue"
        self._processing = f"{PREFIX}{queue_type}:processing"
        self._seen = f"{PREFIX}{queue_type}:seen"
        self._done = f"{PREFIX}{queue_type}:done"
        self._failed = f"{PREFIX}{queue_type}:failed"
        self._attempts = f"{PREFIX}{queue_type}:attempts"

    # ---- 任务 id 构造 (对外入口) ----

    @staticmethod
    def make_keyword_task(city: str, kw: str) -> str:
        """关键词搜索任务 (每"城市×关键词"组合一个任务, 消费时自动翻完所有页)。"""
        return f"{TASK_KEYWORD}:{city}:{kw}"

    @staticmethod
    def make_company_task(company_number: str) -> str:
        return f"{TASK_COMPANY}:{company_number}"

    @staticmethod
    def make_position_task(number: str) -> str:
        return f"{TASK_POSITION}:{number}"

    @staticmethod
    def task_type(task_id: str) -> str:
        """从任务 id 提取类型前缀 (keyword/company/position)。"""
        return task_id.split(":", 1)[0]

    # ---- producer ----

    async def enqueue(self, task_id: str) -> bool:
        """入队; 返回是否新任务 (去重)。"""
        if await self.redis.sadd(self._seen, task_id):
            await self.redis.rpush(self._queue, task_id)
            return True
        return False

    async def enqueue_many(self, task_ids: Iterable[str]) -> int:
        """批量入队 (去重), 返回新增数。接受 list/tuple/生成器。"""
        added = 0
        for tid in task_ids:
            if await self.enqueue(tid):
                added += 1
        return added

    async def enqueue_many_batch(self, task_ids: Iterable[str]) -> int:
        """批量入队 (pipeline, 1 次 RTT): 先去重再 RPUSH, 返回新增数。

        网络往返从 O(n) 降到 O(1) (搜索页每页 ~14 公司/岗位, 快一个量级)。
        不检查 failed (职位号无重试上限语义); 语义与逐条 enqueue 等价。
        """
        tids = list(task_ids)
        if not tids:
            return 0
        async with self.redis.pipeline(transaction=False) as pipe:
            for tid in tids:
                pipe.sadd(self._seen, tid)
            results = await pipe.execute()
        added = [tid for tid, r in zip(tids, results) if r]
        if added:
            await self.redis.rpush(self._queue, *added)
        return len(added)

    async def enqueue_new_not_failed(self, task_ids: Iterable[str]) -> int:
        """批量入队 (pipeline, 2 次 RTT): 跳过 failed 且未 seen 的任务, 返回新增数。

        语义等价于逐条 `not is_failed(t) and enqueue(t)` (company: 补采任务防失败风暴)。
        """
        tids = list(task_ids)
        if not tids:
            return 0
        async with self.redis.pipeline(transaction=False) as pipe:
            for tid in tids:
                pipe.sismember(self._failed, tid)
            res_f = await pipe.execute()
        fresh = [tid for tid, f in zip(tids, res_f) if not f]
        if not fresh:
            return 0
        async with self.redis.pipeline(transaction=False) as pipe:
            for tid in fresh:
                pipe.sadd(self._seen, tid)
            res_s = await pipe.execute()
        new_ids = [tid for tid, r in zip(fresh, res_s) if r]
        if new_ids:
            await self.redis.rpush(self._queue, *new_ids)
        return len(new_ids)

    # ---- consumer ----

    async def dequeue(self, timeout: int = 1) -> Optional[str]:
        """阻塞取一个任务 (BLPOP), 标记 processing (记录领取时间)。"""
        item = await self.redis.blpop(self._queue, timeout=timeout)
        if not item:
            return None
        task_id = item[1]
        await self.redis.hset(self._processing, task_id, str(time.time()))
        return task_id

    async def complete(self, task_id: str) -> None:
        await self.redis.hdel(self._processing, task_id)
        await self.redis.sadd(self._done, task_id)

    async def is_failed(self, task_id: str) -> bool:
        """任务是否已重试超限标记 failed (防止 keyword 消费反复拉起失败公司)。"""
        return bool(await self.redis.sismember(self._failed, task_id))

    async def fail(self, task_id: str, max_attempts: int = 3) -> bool:
        """失败: 尝试次数内重新入队, 否则标记 failed。返回是否重试。

        max_attempts = 总尝试上限 (第 1 次失败 = 尝试 1); 达到上限即标记 failed。
        """
        await self.redis.hdel(self._processing, task_id)
        n = await self.redis.hincrby(self._attempts, task_id, 1)
        await self.redis.expire(self._attempts, ATTEMPTS_TTL)   # TTL 兜底防泄漏
        if n < max_attempts:
            await self.redis.rpush(self._queue, task_id)
            logger.warning("任务 %s 第 %d 次失败, 重新入队", task_id, n)
            return True
        await self.redis.sadd(self._failed, task_id)
        await self.redis.hdel(self._attempts, task_id)           # 清理计数
        logger.warning("任务 %s 失败 %d 次, 标记 failed", task_id, n)
        return False

    async def fail_permanent(self, task_id: str) -> None:
        """永久失败: 直接标记 failed, 不入队重试 (岗位失效等不可恢复错误)。

        区分可重试/不可重试: 失败风暴时避免无意义重试占队列、刷错误日志。
        """
        await self.redis.hdel(self._processing, task_id)
        await self.redis.sadd(self._failed, task_id)
        await self.redis.hdel(self._attempts, task_id)

    async def recover_stale(self, max_age: int = STALE_MAX_AGE) -> int:
        """回收崩溃 worker 的在途任务 (分布式锁保证单实例执行)。

        扫描 processing, 领取时间超过 max_age 的任务重新入队。
        """
        lock = f"{LOCK_PREFIX}recover:{self.type}"
        if not await self.redis.set(lock, "1", nx=True, ex=30):
            return 0
        try:
            now = time.time()
            rec = await self.redis.hgetall(self._processing)
            stale = [tid for tid, ts in rec.items()
                     if now - float(ts) > max_age]
            for tid in stale:
                await self.redis.hdel(self._processing, tid)
                await self.redis.rpush(self._queue, tid)
            if stale:
                logger.warning("回收 %d 个崩溃在途任务, 重新入队", len(stale))
            return len(stale)
        finally:
            await self.redis.delete(lock)

    async def is_drained(self) -> bool:
        """队列空且无处理中任务 (worker 退出判定)。"""
        return (await self.redis.llen(self._queue)) == 0 and \
               (await self.redis.hlen(self._processing)) == 0

    # ---- stats / 运维 ----

    async def stats(self) -> dict:
        return {
            "queue": await self.redis.llen(self._queue),
            "processing": await self.redis.hlen(self._processing),
            "seen": await self.redis.scard(self._seen),
            "done": await self.redis.scard(self._done),
            "failed": await self.redis.scard(self._failed),
        }

    async def clear(self) -> None:
        """清空本队列类型的所有任务键 (重建任务池用)。

        用 SCAN 游标而非 KEYS (KEYS 在生产阻塞 Redis 所有客户端)。
        """
        n = 0
        async for key in self.redis.scan_iter(f"{PREFIX}{self.type}:*"):
            await self.redis.delete(key)
            n += 1
        logger.info("队列 %s 已清空 (%d 键)", self.type, n)

    async def close(self) -> None:
        await self.redis.aclose()
