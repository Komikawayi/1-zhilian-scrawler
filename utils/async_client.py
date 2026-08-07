# -*- coding: utf-8 -*-
"""
异步 HTTP 客户端 — curl_cffi AsyncSession + 全局令牌桶限速

Phase A 高并发详情采集 (详情并发 10, 无 IP 信誉依赖)。

保持与 utils/http_client.py 相同的风控纪律:
  - impersonate=chrome (TLS 指纹)
  - AsyncRateLimiter 令牌桶: 跨 worker 全局限速 (防 IP 信誉)
  - 重试 + 超时 (settings)
  - 集成 RiskState (async-safe, 见 utils/risk.py)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Optional

from curl_cffi import requests as cffi_requests

from config import settings

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """令牌桶限速器: 限制全局每秒请求数 (跨所有 worker 共享)。rate<=0 不限速。"""

    def __init__(self, rate_per_sec: float):
        self.rate = max(rate_per_sec, 0.0)
        self.capacity = max(self.rate, 1.0)
        self.tokens = self.capacity
        self.updated = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            # 锁外等待, 不阻塞其他 waiter
            await asyncio.sleep(wait)


class RedisRateLimiter:
    """跨进程全局限速 (Redis 滑动窗口, Lua 原子)。避免固定窗口边界 2x 突发。

    窗口 = 1s, 容量 = rate_per_sec。ZSET 记录时间戳, 过期成员自动清理。
    """

    _LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window_ms)
local count = redis.call('ZCARD', key)
if count < limit then
  local seq = redis.call('INCR', key .. ':seq')
  redis.call('ZADD', key, now, now .. '-' .. seq)
  redis.call('EXPIRE', key, math.ceil(window_ms / 1000))
  redis.call('EXPIRE', key .. ':seq', math.ceil(window_ms / 1000))
  return 1
end
return 0
"""

    def __init__(self, redis, rate_per_sec: float):
        self.redis = redis
        self.rate = max(rate_per_sec, 0.0)
        self.key = "zhaopin:ratelimit:sw"

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        window_ms = 1000
        while True:
            ok = await self.redis.eval(self._LUA, 1, self.key,
                                       int(time.time() * 1000), window_ms, self.rate)
            if ok:
                return
            await asyncio.sleep(0.02)


class AsyncZhilianClient:
    """异步纯协议客户端 (curl_cffi AsyncSession)。"""

    def __init__(
        self,
        impersonate: str = "chrome",
        risk=None,
        rate_limiter: Optional[AsyncRateLimiter] = None,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.impersonate = impersonate
        self.risk = risk
        self.rate_limiter = rate_limiter
        self.retries = retries or settings.RETRIES
        self.timeout = timeout or settings.TIMEOUT
        self.session = cffi_requests.AsyncSession(impersonate=impersonate)

    async def get(self, url: str, **kw) -> cffi_requests.Response:
        return await self._request("get", url, **kw)

    async def post(self, url: str, **kw) -> cffi_requests.Response:
        return await self._request("post", url, **kw)

    async def _request(self, method: str, url: str, **kw) -> cffi_requests.Response:
        """统一入口: 冷却阻塞 → 限速 → 请求 → 重试。"""
        if self.risk is not None:
            await self.risk.async_await_cooldown()
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                return await getattr(self.session, method)(url, timeout=self.timeout, **kw)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("async %s %s attempt %d/%d 失败: %s",
                               method, url[:80], attempt, self.retries, str(e)[:80])
                await asyncio.sleep(random.uniform(1.5, 4.0) * attempt)
        raise ConnectionError(f"异步请求失败 {url[:100]}: {last_err}")

    # ---- 风控上报 (委托给 risk, async-safe) ----
    async def report_success(self) -> None:
        if self.risk is not None:
            await self.risk.async_on_success()

    async def report_challenge(self) -> None:
        if self.risk is not None:
            await self.risk.async_on_challenge()

    async def report_captcha(self) -> None:
        if self.risk is not None:
            await self.risk.async_on_captcha()

    async def close(self) -> None:
        await self.session.close()


# ---- 异步 fe-api 详情抓取 (路线一, 镜像 utils/fe_api.py 逻辑) ----

DETAIL_V2_URL = "https://fe-api.zhaopin.com/c/i/jobs/position-detailv2"

_FE_API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-zp-business-system": "1",
    "x-zp-page-code": "4019",
    "x-zp-platform": "13",
    "referer": "https://www.zhaopin.com/",
}


def _fe_api_params(number: str) -> dict:
    """fe-api 动态参数 (非签名, 随机即可)。"""
    return {
        "_v": "%.8f" % (time.time() % 1),
        "x-zp-page-request-id": uuid.uuid4().hex + "-" + str(int(time.time() * 1000)),
        "x-zp-client-id": str(uuid.uuid4()),
        "platform": "13",
        "version": "0.0.0",
        "number": number,
    }


async def fetch_position_detail_v2_async(client: AsyncZhilianClient, number: str) -> dict:
    """异步拉取职位详情 (position-detailv2, 匿名无挑战)。返回 data 部分。"""
    resp = await client.get(DETAIL_V2_URL, headers=_FE_API_HEADERS, params=_fe_api_params(number))
    if resp.status_code != 200:
        raise ConnectionError(f"position-detailv2 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except json.JSONDecodeError as e:
        raise ConnectionError(f"position-detailv2 响应非 JSON: {e}") from e
    if body.get("code") != 200 or body.get("apiCode") != 200:
        raise ConnectionError(
            f"position-detailv2 业务错误 code={body.get('code')} apiCode={body.get('apiCode')} "
            f"msg={body.get('message')}")
    return body.get("data") or {}
