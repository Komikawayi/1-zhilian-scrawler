# -*- coding: utf-8 -*-
"""
单机异步流水线 — 搜索生产者 + 详情消费者 + PostgreSQL 入库

Phase A 核心: 详情并发 (默认 400, 无 IP 信誉依赖), 搜索并发 (默认 100, 风控敏感)。

链路:
  搜索 SSR (受 search rate 桶控制, ≤search_concurrency) → 唯一 number → asyncio.Queue(有界, 背压)
    → 详情并发拉取 (detail_concurrency) → upsert PostgreSQL (positions/companies)

特性:
  - 全局限速器 (AsyncRateLimiter, 跨 worker 共享)
  - 风控状态机 (async-safe): 挑战/验证码自动冷却
  - resume: 跳过已入库 number (中断恢复)
  - 优雅停机: 取消时保存 run 统计
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, List

from config import settings
from utils.async_client import (
    AsyncRateLimiter, AsyncZhilianClient, fetch_position_detail_v2_async,
)
from utils.latency_stats import LatencyStats
from utils.parser import extract_initial_state, parse_position_detail_v2
from utils.risk import RiskState
from utils.storage_pg import AsyncStorage

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    keywords: List[str]
    city: str
    pages: int = 5
    detail_concurrency: int = settings.DETAIL_CONCURRENCY
    search_concurrency: int = settings.SEARCH_CONCURRENCY
    queue_size: int = 200
    detail_rate_per_sec: float = settings.DETAIL_RATE_PER_SEC
    db_url: str = ""          # PostgreSQL URL (asyncpg); 空则用 settings.DB_URL
    resume: bool = False
    name: str = ""


class Pipeline:
    """asyncio 采集流水线 (搜索 → 队列 → 详情并发 → PostgreSQL)。"""

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.storage: AsyncStorage = None
        self.risk = RiskState()
        self.rate_limiter = AsyncRateLimiter(cfg.detail_rate_per_sec)
        self.stats = LatencyStats()   # 分环节耗时统计 (结束总结)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_size)
        self.total = 0          # 入队 number 数 (搜索池唯一)
        self.success = 0
        self.fail = 0
        self.started = 0.0
        self._seen: set = set()     # 唯一 number (去重 + resume)
        self._search_sem = asyncio.Semaphore(cfg.search_concurrency)

    async def run(self) -> Dict:
        """执行流水线, 返回统计。"""
        self.started = time.time()
        from config import settings as _settings
        self.storage = await AsyncStorage.create(
            self.cfg.db_url or _settings.DB_URL, pool_max=_settings.DB_POOL_MAX)
        if self.cfg.resume:
            existing = await self.storage.get_existing_numbers()
            self._seen = existing
            logger.info("resume: 跳过已入库 %d 个 number", len(existing))
        params = {"keywords": self.cfg.keywords, "city": self.cfg.city, "pages": self.cfg.pages,
                  "concurrency": self.cfg.detail_concurrency, "resume": self.cfg.resume,
                  "name": self.cfg.name}
        run_id = await self.storage.start_run("pipeline", params)
        # 搜索/详情各一个 client (name 区分统计 stage), 共享同一限速器
        search_client = AsyncZhilianClient(
            risk=self.risk, rate_limiter=self.rate_limiter,
            stats=self.stats, name="search",
            max_clients=max(1, self.cfg.search_concurrency))
        detail_client = AsyncZhilianClient(
            risk=self.risk, rate_limiter=self.rate_limiter,
            stats=self.stats, name="detail",
            max_clients=max(1, self.cfg.detail_concurrency))
        try:
            producers = [asyncio.create_task(self._search_worker(search_client, kw))
                         for kw in self.cfg.keywords]
            consumers = [asyncio.create_task(self._detail_worker(detail_client))
                         for _ in range(self.cfg.detail_concurrency)]
            await asyncio.gather(*producers)
            for _ in consumers:
                await self.queue.put(None)          # 哨兵: 通知消费者收尾
            await asyncio.gather(*consumers)
            self.stats.print_summary(0)             # 结束总结
        except asyncio.CancelledError:
            logger.warning("流水线被取消, 保存进度...")
        finally:
            await search_client.close()
            await detail_client.close()
            await self.storage.finish_run(run_id, self.total, self.success, self.fail)
            await self.storage.close()
        elapsed = time.time() - self.started
        rate = (self.success + self.fail) / elapsed if elapsed else 0
        logger.info("流水线完成: 成功%d 失败%d 总耗时%.0fs (%.2f条/s) 风控=%s",
                    self.success, self.fail, elapsed, rate, self.risk.state)
        return {"run_id": run_id, "total": self.total, "success": self.success,
                "fail": self.fail, "elapsed": elapsed, "rate": rate,
                "risk_state": self.risk.state}

    # ---- 搜索生产者 ----

    async def _search_worker(self, client: AsyncZhilianClient, kw: str) -> None:
        """受风控桶控制的搜索: SSR 提取唯一 number 入队。"""
        for page in range(1, self.cfg.pages + 1):
            try:
                html = await self._search_page(client, kw, self.cfg.city, page)
                t0 = time.perf_counter()
                state = extract_initial_state(html)
                self.stats.record("search_parse", time.perf_counter() - t0)
                items = state.get("positionList") or []
                for item in items:
                    num = item.get("number") if isinstance(item, dict) else None
                    if not num or num in self._seen:
                        continue
                    self._seen.add(num)
                    self.total += 1
                    await self.queue.put(num)
            except Exception as e:  # noqa: BLE001
                logger.warning("搜索 %s p%d 失败: %s", kw, page, str(e)[:120])
            await asyncio.sleep(0.2)

    async def _search_page(self, client: AsyncZhilianClient, kw: str, city: str, page: int) -> str:
        """搜索页（指定页码，防止入口 cookie 将其重定向回第 1 页）。"""
        async with self._search_sem:
            return await client.fetch_search_page(city, kw, page)

    # ---- 详情消费者 ----

    async def _detail_worker(self, client: AsyncZhilianClient) -> None:
        """高并发详情拉取: 队列取 number → position-detailv2 → upsert。"""
        while True:
            num = await self.queue.get()
            if num is None:
                self.queue.task_done()
                break
            try:
                data = await fetch_position_detail_v2_async(client, num)
                t0 = time.perf_counter()
                row = parse_position_detail_v2(data)
                self.stats.record("detail_parse", time.perf_counter() - t0)
                row["position_number"] = num
                row["source"] = "detailv2"
                row["raw_json"] = json.dumps(data, ensure_ascii=False)[:8000]
                await client.report_success()
                t0 = time.perf_counter()
                comp = {k: row.get(k, "") for k in
                        ("company_number", "company_name", "company_size",
                         "financing_stage", "industry_name")}
                await self.storage.upsert_position_and_company(row, comp)
                self.stats.record("db_detail", time.perf_counter() - t0)
                self.success += 1
            except Exception as e:  # noqa: BLE001
                self.fail += 1
                logger.warning("详情 %s 失败: %s", num, str(e)[:100])
            finally:
                self.queue.task_done()
                done = self.success + self.fail
                if done % 100 == 0:
                    el = time.time() - self.started
                    logger.info("CHECKPOINT %d: 成功%d 失败%d 耗时%.0fs (%.2f/s) 风控=%s",
                                done, self.success, self.fail, el,
                                done / el if el else 0, self.risk.state)
