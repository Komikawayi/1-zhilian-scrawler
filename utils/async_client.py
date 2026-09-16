# -*- coding: utf-8 -*-
"""
异步 HTTP 客户端 — curl_cffi AsyncSession + 全局令牌桶限速

Phase A 高并发详情采集 (详情并发默认 400, 无 IP 信誉依赖)。

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
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi.const import CurlInfo

from config import settings
from utils.latency_stats import LatencyStats

logger = logging.getLogger(__name__)

# 网络请求要采集的 CURLINFO 分段 (只取这两个; 不采 DNS/TCP/TLS, keep-alive 稳态无意义)
_NET_INFOS = [CurlInfo.STARTTRANSFER_TIME, CurlInfo.TOTAL_TIME]


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
    """跨进程全局匀速限速器（Redis Lua 原子分配请求时隙）。"""

    _LUA = """
local key = KEYS[1]
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local rate = tonumber(ARGV[1])
local interval_ms = 1000 / rate
local next_at = tonumber(redis.call('GET', key .. ':next_at') or now)
if next_at < now then
  next_at = now
end
local wait_ms = next_at - now
local following = next_at + interval_ms
redis.call('SET', key .. ':next_at', following,
           'PX', math.max(2000, math.ceil(following - now + 1000)))
redis.call('INCR', key .. ':issued')
return math.floor(wait_ms)
"""

    def __init__(self, redis, rate_per_sec: float, key: str = "zhaopin:ratelimit:sw"):
        self.redis = redis
        self.rate = max(rate_per_sec, 0.0)
        self.key = key
        self._pending_responses = 0

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        wait_ms = int(await self.redis.eval(self._LUA, 1, self.key, self.rate))
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

    async def record_response(self) -> None:
        """Record a completed HTTP response for global throughput monitoring."""
        self._pending_responses += 1
        if self._pending_responses >= 32:
            await self.flush_response_metrics()

    async def flush_response_metrics(self) -> None:
        pending = self._pending_responses
        self._pending_responses = 0
        if pending:
            await self.redis.incrby(self.key + ":responded", pending)

    async def request_counts(self) -> tuple[int, int]:
        await self.flush_response_metrics()
        issued, responded = await self.redis.mget(
            self.key + ":issued", self.key + ":responded")
        return int(issued or 0), int(responded or 0)


class AsyncZhilianClient:
    """异步纯协议客户端 (curl_cffi AsyncSession)。"""

    def __init__(
        self,
        impersonate: str = "chrome",
        risk=None,
        rate_limiter: Optional[AsyncRateLimiter] = None,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
        stats: Optional[LatencyStats] = None,
        name: str = "",
        max_clients: Optional[int] = None,
    ) -> None:
        self.impersonate = impersonate
        self.risk = risk
        self.rate_limiter = rate_limiter
        self.retries = retries or settings.RETRIES
        self.timeout = timeout or settings.TIMEOUT
        self.stats = stats
        self.name = name            # 统计 stage: "search" / "detail"
        # curl_infos: 每个响应自动携带 TTFB + 总耗时 (requests 层行为不变)
        self.max_clients = max(1, max_clients or 10)
        self.session = cffi_requests.AsyncSession(
            impersonate=impersonate, max_clients=self.max_clients,
            curl_infos=_NET_INFOS)
        # AsyncSession 共用 cookie jar。搜索页的两跳导航必须串行，否则另一个任务
        # 写入的路由 cookie 会令当前页重定向至不含页码的 /jobs。
        self._search_navigation_lock = asyncio.Lock()

    async def get(self, url: str, **kw) -> cffi_requests.Response:
        return await self._request("get", url, **kw)

    async def post(self, url: str, **kw) -> cffi_requests.Response:
        return await self._request("post", url, **kw)

    async def _request(self, method: str, url: str, **kw) -> cffi_requests.Response:
        """统一入口: 冷却阻塞 → 限速 → 请求 → 重试。"""
        if self.risk is not None:
            t0 = time.perf_counter()
            waited = await self.risk.async_await_cooldown()
            if self.stats is not None:
                self.stats.record("risk_check", time.perf_counter() - t0 - waited)
                if waited > 0:
                    self.stats.record("cooldown", waited)
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = await getattr(self.session, method)(url, timeout=self.timeout, **kw)
                record_response = getattr(self.rate_limiter, "record_response", None)
                if record_response is not None:
                    await record_response()
                if self.stats is not None and self.name:
                    infos = getattr(resp, "infos", None) or {}
                    self.stats.record_net(self.name,
                                          infos.get(CurlInfo.TOTAL_TIME),
                                          infos.get(CurlInfo.STARTTRANSFER_TIME))
                return resp
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
        flush = getattr(self.rate_limiter, "flush_response_metrics", None)
        if flush is not None:
            await flush()
        await self.session.close()

    async def fetch_search_page(self, city: str, keyword: str, page: int) -> str:
        """获取指定 SSR 搜索页，并阻止路由 cookie 将页码重置为第 1 页。"""
        query = quote(keyword)
        entry_url = (f"https://sou.zhaopin.com/?jl={city}&kw={query}&p={page}"
                     if city != "0" else f"https://sou.zhaopin.com/?kw={query}&p={page}")
        async with self._search_navigation_lock:
            redirect = await self.get(entry_url, allow_redirects=False)
            if redirect.status_code not in (301, 302, 303, 307, 308):
                raise ConnectionError(f"搜索入口 HTTP {redirect.status_code}")
            seo_url = redirect.headers.get("location")
            if not seo_url:
                raise ConnectionError("搜索入口缺少跳转地址")
            self.session.cookies.clear()
            response = await self.get(seo_url, allow_redirects=False)

        if response.status_code != 200:
            raise ConnectionError(f"搜索页 HTTP {response.status_code}")
        text = response.text
        if "Security Verification" in text:
            await self.report_captcha()
            raise ConnectionError("EdgeOne 拦截 (验证码)")
        if "__INITIAL_STATE__" not in text:
            await self.report_challenge()
            raise ConnectionError("响应缺少 __INITIAL_STATE__")
        await self.report_success()
        return text


# ---- 异步 fe-api 详情抓取 (路线一, 镜像 utils/fe_api.py 逻辑) ----

DETAIL_V2_URL = "https://fe-api.zhaopin.com/c/i/jobs/position-detailv2"

_FE_API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-zp-business-system": "1",
    "x-zp-page-code": "4019",
    "x-zp-platform": "13",
    "referer": "https://www.zhaopin.com/",
}


class PositionUnavailableError(ConnectionError):
    """岗位详情暂不可用 (apiCode=211)。

    211 通常表示下架/过期，但高并发实测存在偶发 211 后恢复 200，消费方需有限重试。
    """


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
        if body.get("apiCode") == 211:
            raise PositionUnavailableError(
                f"position-detailv2 暂不可用 apiCode=211 {number}")
        raise ConnectionError(
            f"position-detailv2 业务错误 code={body.get('code')} apiCode={body.get('apiCode')} "
            f"msg={body.get('message')}")
    return body.get("data") or {}
