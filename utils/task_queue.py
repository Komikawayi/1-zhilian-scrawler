# -*- coding: utf-8 -*-
"""
Redis 分布式任务队列, 搜索/详情双队列。

任务状态使用 Redis Lua 脚本保持原子性。领取时先用 BLMOVE 把任务移到
processing staging list，再写租约时间和 claim token；即使 worker 在写租约前退出，
watchdog 仍能发现 staging list 中的任务并回收，旧 token 也不能操作新租约。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

PREFIX = "zhaopin:tasks:"
LOCK_PREFIX = "zhaopin:lock:"
DEFAULT_URL = "redis://127.0.0.1:6379/0"

QUEUE_SEARCH = "search"
QUEUE_POSITION = "position"
QUEUE_TYPES = (QUEUE_SEARCH, QUEUE_POSITION)

TASK_KEYWORD = "keyword"
TASK_COMPANY = "company"
TASK_POSITION = "position"

STALE_MAX_AGE = 120
LOCK_TTL = 30
ATTEMPTS_TTL = 86400


class TaskClaim(str):
    """行为与 task_id 字符串一致，同时携带本次领取的 fencing token。"""

    token: str

    def __new__(cls, task_id: str, token: str):
        claim = super().__new__(cls, task_id)
        claim.token = token
        return claim


class TaskQueue:
    """Redis 异步任务队列 (单类型, 崩溃安全)。"""

    _ENQUEUE_LUA = """
local added = 0
for i = 1, #ARGV do
  if redis.call('SADD', KEYS[1], ARGV[i]) == 1 then
    redis.call('RPUSH', KEYS[2], ARGV[i])
    added = added + 1
  end
end
return added
"""

    _ENQUEUE_NOT_FAILED_LUA = """
local added = 0
for i = 1, #ARGV do
  local tid = ARGV[i]
  if redis.call('SISMEMBER', KEYS[1], tid) == 0 and
     redis.call('SADD', KEYS[2], tid) == 1 then
    redis.call('RPUSH', KEYS[3], tid)
    added = added + 1
  end
end
return added
"""

    _CLAIM_LUA = """
if not redis.call('LPOS', KEYS[1], ARGV[1]) then
  return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('HSET', KEYS[2], ARGV[1], now)
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
return 1
"""

    _COMPLETE_LUA = """
local token = redis.call('HGET', KEYS[3], ARGV[1])
if not token or token ~= ARGV[2] then
  return 0
end
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
if removed > 0 then
  redis.call('INCR', KEYS[4])
end
return removed
"""

    _FAIL_LUA = """
local tid = ARGV[1]
local token = redis.call('HGET', KEYS[3], tid)
if not token or token ~= ARGV[2] then
  return -1
end
local max_attempts = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
redis.call('LREM', KEYS[1], 1, tid)
redis.call('HDEL', KEYS[2], tid)
redis.call('HDEL', KEYS[3], tid)
local n = redis.call('HINCRBY', KEYS[5], tid, 1)
redis.call('EXPIRE', KEYS[5], ttl)
if n < max_attempts then
  redis.call('RPUSH', KEYS[4], tid)
  return n
end
redis.call('SADD', KEYS[6], tid)
redis.call('HDEL', KEYS[5], tid)
return 0
"""

    _FAIL_PERMANENT_LUA = """
local token = redis.call('HGET', KEYS[3], ARGV[2])
if not token or token ~= ARGV[1] then
  return 0
end
local tid = ARGV[2]
local removed = redis.call('LREM', KEYS[1], 1, tid)
redis.call('HDEL', KEYS[2], tid)
redis.call('HDEL', KEYS[3], tid)
redis.call('SADD', KEYS[4], tid)
redis.call('HDEL', KEYS[5], tid)
return removed
"""

    _RECOVER_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local max_age = tonumber(ARGV[1])
local recovered = 0
local items = redis.call('LRANGE', KEYS[1], 0, -1)
for _, tid in ipairs(items) do
  local ts = redis.call('HGET', KEYS[2], tid)
  if not ts or now - tonumber(ts) > max_age then
    if redis.call('LREM', KEYS[1], 1, tid) > 0 then
      redis.call('HDEL', KEYS[2], tid)
      redis.call('HDEL', KEYS[4], tid)
      redis.call('RPUSH', KEYS[3], tid)
      recovered = recovered + 1
    end
  end
end
return recovered
"""

    _UNLOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

    _HEARTBEAT_LUA = """
if redis.call('LPOS', KEYS[1], ARGV[1]) and
   redis.call('HGET', KEYS[3], ARGV[1]) == ARGV[2] then
  local clock = redis.call('TIME')
  local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
  redis.call('HSET', KEYS[2], ARGV[1], now)
  return 1
end
return 0
"""

    _MIGRATE_PROCESSING_LUA = """
local migrated = 0
local tids = redis.call('HKEYS', KEYS[1])
for _, tid in ipairs(tids) do
  if not redis.call('LPOS', KEYS[2], tid) then
    redis.call('RPUSH', KEYS[2], tid)
    migrated = migrated + 1
  end
  redis.call('HSETNX', KEYS[3], tid, 'legacy:' .. tid)
end
return migrated
"""

    _SEED_DONE_LUA = """
local current = redis.call('GET', KEYS[1])
if current then
  return current
end
local legacy = redis.call('SCARD', KEYS[2])
redis.call('SET', KEYS[1], legacy)
redis.call('DEL', KEYS[2])
return legacy
"""

    def __init__(self, redis_url: str = DEFAULT_URL,
                 queue_type: str = QUEUE_POSITION) -> None:
        if queue_type not in QUEUE_TYPES:
            raise ValueError(f"queue_type 必须 ∈ {QUEUE_TYPES}, got {queue_type!r}")
        self.type = queue_type
        self.redis = aioredis.Redis.from_url(redis_url, decode_responses=True)
        self._queue = f"{PREFIX}{queue_type}:queue"
        self._processing_list = f"{PREFIX}{queue_type}:processing:list"
        self._processing = f"{PREFIX}{queue_type}:processing"
        self._claim_tokens = f"{PREFIX}{queue_type}:claim_tokens"
        self._seen = f"{PREFIX}{queue_type}:seen"
        self._done = f"{PREFIX}{queue_type}:done"  # legacy set, migrated lazily
        self._done_count = f"{PREFIX}{queue_type}:done_count"
        self._failed = f"{PREFIX}{queue_type}:failed"
        self._attempts = f"{PREFIX}{queue_type}:attempts"
        self._claims: dict[str, str] = {}

    @staticmethod
    def make_keyword_task(city: str, kw: str) -> str:
        return f"{TASK_KEYWORD}:{city}:{kw}"

    @staticmethod
    def make_company_task(company_number: str) -> str:
        return f"{TASK_COMPANY}:{company_number}"

    @staticmethod
    def make_position_task(number: str) -> str:
        return f"{TASK_POSITION}:{number}"

    @staticmethod
    def task_type(task_id: str) -> str:
        return task_id.split(":", 1)[0]

    async def enqueue(self, task_id: str) -> bool:
        added = await self.redis.eval(self._ENQUEUE_LUA, 2,
                                      self._seen, self._queue, task_id)
        return bool(added)

    async def enqueue_many(self, task_ids: Iterable[str]) -> int:
        return await self.enqueue_many_batch(task_ids)

    async def enqueue_many_batch(self, task_ids: Iterable[str]) -> int:
        tids = list(task_ids)
        if not tids:
            return 0
        return int(await self.redis.eval(self._ENQUEUE_LUA, 2,
                                         self._seen, self._queue, *tids))

    async def enqueue_new_not_failed(self, task_ids: Iterable[str]) -> int:
        tids = list(task_ids)
        if not tids:
            return 0
        return int(await self.redis.eval(self._ENQUEUE_NOT_FAILED_LUA, 3,
                                         self._failed, self._seen,
                                         self._queue, *tids))

    async def dequeue(self, timeout: int = 1) -> Optional[TaskClaim]:
        """原子移入 staging list，再写领取时间。"""
        while True:
            task_id = await self.redis.blmove(self._queue, self._processing_list,
                                              timeout=timeout, src="LEFT",
                                              dest="RIGHT")
            if task_id is None:
                return None
            token = uuid.uuid4().hex
            claimed = await self.redis.eval(
                self._CLAIM_LUA, 3, self._processing_list, self._processing,
                self._claim_tokens, task_id, token)
            if claimed:
                self._claims[task_id] = token
                return TaskClaim(task_id, token)
            # A concurrent stale-recovery may have moved this staging item back.
            # Retry the dequeue so the task is not lost.

    def claim_token(self, task_id: str) -> Optional[str]:
        """返回本实例最近领取的租约 token, 供跨层传递或诊断使用。"""
        return getattr(task_id, "token", None) or self._claims.get(task_id)

    def _resolve_token(self, task_id: str, token: Optional[str]) -> Optional[str]:
        return token or getattr(task_id, "token", None) or self._claims.get(task_id)

    def _forget_claim(self, task_id: str, token: str) -> None:
        if self._claims.get(task_id) == token:
            self._claims.pop(task_id, None)

    async def complete(self, task_id: str, token: Optional[str] = None) -> bool:
        token = self._resolve_token(task_id, token)
        if not token:
            return False
        removed = await self.redis.eval(self._COMPLETE_LUA, 4,
                              self._processing_list, self._processing,
                              self._claim_tokens, self._done_count,
                              task_id, token)
        self._forget_claim(task_id, token)
        return bool(removed)

    async def heartbeat(self, task_id: str, token: Optional[str] = None) -> bool:
        """续租在途任务；任务已被回收时不重新创建 processing 记录。"""
        token = self._resolve_token(task_id, token)
        if not token:
            return False
        updated = await self.redis.eval(self._HEARTBEAT_LUA, 3,
                                        self._processing_list,
                                        self._processing, self._claim_tokens,
                                        task_id, token)
        return bool(updated)

    async def _migrate_legacy_processing(self) -> int:
        """把旧版本仅存在于 timestamp hash 的在途任务补入 staging list。"""
        return int(await self.redis.eval(self._MIGRATE_PROCESSING_LUA, 3,
                                         self._processing,
                                         self._processing_list,
                                         self._claim_tokens))

    async def is_failed(self, task_id: str) -> bool:
        return bool(await self.redis.sismember(self._failed, task_id))

    async def fail(self, task_id: str, max_attempts: int = 3) -> Optional[bool]:
        token = self._resolve_token(task_id, None) or ""
        attempt = await self.redis.eval(self._FAIL_LUA, 6,
                                        self._processing_list, self._processing,
                                        self._claim_tokens, self._queue,
                                        self._attempts, self._failed,
                                        task_id, token, max_attempts, ATTEMPTS_TTL)
        self._forget_claim(task_id, token)
        if attempt == -1:
            return None
        if attempt:
            logger.warning("任务 %s 第 %s 次失败, 重新入队", task_id, attempt)
            return True
        logger.warning("任务 %s 失败, 标记 failed", task_id)
        return False

    async def fail_permanent(self, task_id: str, token: Optional[str] = None) -> bool:
        token = self._resolve_token(task_id, token)
        if not token:
            return False
        removed = await self.redis.eval(self._FAIL_PERMANENT_LUA, 5,
                              self._processing_list, self._processing,
                              self._claim_tokens, self._failed, self._attempts,
                              token, task_id)
        self._forget_claim(task_id, token)
        return bool(removed)

    async def recover_stale(self, max_age: int = STALE_MAX_AGE) -> int:
        lock = f"{LOCK_PREFIX}recover:{self.type}"
        token = uuid.uuid4().hex
        if not await self.redis.set(lock, token, nx=True, ex=LOCK_TTL):
            return 0
        try:
            await self._migrate_legacy_processing()
            recovered = int(await self.redis.eval(
                self._RECOVER_LUA, 4, self._processing_list,
                self._processing, self._queue, self._claim_tokens,
                max_age))
            if recovered:
                logger.warning("回收 %d 个崩溃在途任务, 重新入队", recovered)
            return recovered
        finally:
            await self.redis.eval(self._UNLOCK_LUA, 1, lock, token)

    async def is_drained(self) -> bool:
        await self._migrate_legacy_processing()
        return (await self.redis.llen(self._queue)) == 0 and \
               (await self.redis.llen(self._processing_list)) == 0

    async def stats(self) -> dict:
        await self._migrate_legacy_processing()
        done = await self.redis.eval(self._SEED_DONE_LUA, 2,
                                     self._done_count, self._done)
        return {
            "queue": await self.redis.llen(self._queue),
            "processing": await self.redis.llen(self._processing_list),
            "seen": await self.redis.scard(self._seen),
            "done": int(done),
            "failed": await self.redis.scard(self._failed),
        }

    async def clear(self) -> None:
        n = 0
        async for key in self.redis.scan_iter(f"{PREFIX}{self.type}:*"):
            await self.redis.delete(key)
            n += 1
        logger.info("队列 %s 已清空 (%d 键)", self.type, n)

    async def close(self) -> None:
        await self.redis.aclose()
