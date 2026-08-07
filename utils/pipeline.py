# -*- coding: utf-8 -*-
"""
单机异步流水线 — 搜索生产者 + 详情消费者 + SQLite 入库

Phase A 核心: 详情并发 (默认 10, 无 IP 信誉依赖), 搜索低频 (风控敏感)。

链路:
  搜索 SSR (低频, ≤search_concurrency) → 唯一 number → asyncio.Queue(有界, 背压)
    → 详情并发拉取 (detail_concurrency) → upsert SQLite (positions/companies)

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
from dataclasses import dataclass, field
from typing import Dict, List

from utils.async_client import (
    AsyncRateLimiter, AsyncZhilianClient, fetch_position_detail_v2_async,
)
from utils.parser import extract_initial_state, parse_position_detail_v2
from utils.risk import RiskState
from utils.storage_pg import AsyncStorage

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    keywords: List[str]
    city: str
    pages: int = 5
    detail_concurrency: int = 10
    search_concurrency: int = 2
    queue_size: int = 200
    detail_rate_per_sec: float = 8.0
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
        self.storage = await AsyncStorage.create(self.cfg.db_url or _settings.DB_URL)
        if self.cfg.resume:
            existing = await self.storage.get_existing_numbers()
            self._seen = existing
            logger.info("resume: 跳过已入库 %d 个 number", len(existing))
        params = {"keywords": self.cfg.keywords, "city": self.cfg.city, "pages": self.cfg.pages,
                  "concurrency": self.cfg.detail_concurrency, "resume": self.cfg.resume,
                  "name": self.cfg.name}
        run_id = await self.storage.start_run("pipeline", params)
        client = AsyncZhilianClient(risk=self.risk, rate_limiter=self.rate_limiter)
        try:
            producers = [asyncio.create_task(self._search_worker(client, kw))
                         for kw in self.cfg.keywords]
            consumers = [asyncio.create_task(self._detail_worker(client))
                         for _ in range(self.cfg.detail_concurrency)]
            await asyncio.gather(*producers)
            for _ in consumers:
                await self.queue.put(None)          # 哨兵: 通知消费者收尾
            await asyncio.gather(*consumers)
        except asyncio.CancelledError:
            logger.warning("流水线被取消, 保存进度...")
        finally:
            await client.close()
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
        """低频搜索: SSR 提取唯一 number 入队。"""
        for page in range(1, self.cfg.pages + 1):
            try:
                html = await self._search_page(client, kw, self.cfg.city, page)
                state = extract_initial_state(html)
                items = state.get("positionList") or []
                for item in items:
                    num = item.get("number") if isinstance(item, dict) else None
                    if not num or num in self._seen:
                        continue
                    self._seen.add(num)
                    self.total += 1
                    await self.queue.put(num)
                    await self.storage.upsert_search_pool(
                        num, kw, self.cfg.city, page,
                        item.get("name", "") or "", item.get("companyName", "") or "",
                        item.get("salary60", "") or "")
            except Exception as e:  # noqa: BLE001
                logger.warning("搜索 %s p%d 失败: %s", kw, page, str(e)[:120])
            await asyncio.sleep(0.2)

    async def _search_page(self, client: AsyncZhilianClient, kw: str, city: str, page: int) -> str:
        """搜索页 (镜像 utils/http_client.fetch_search_page 逻辑, async)。"""
        url = f"https://sou.zhaopin.com/?jl={city}&kw={kw}&p={page}"
        async with self._search_sem:
            resp = await client.get(url, allow_redirects=True)
        if resp.status_code != 200:
            raise ConnectionError(f"HTTP {resp.status_code}")
        text = resp.text
        if "Security Verification" in text:
            await client.report_captcha()
            raise ConnectionError("EdgeOne 拦截 (Security Verification)")
        if "__INITIAL_STATE__" not in text:
            await client.report_challenge()
            raise ConnectionError("响应缺少 __INITIAL_STATE__")
        await client.report_success()
        return text

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
                row = parse_position_detail_v2(data)
                row["position_number"] = num
                row["source"] = "detailv2"
                row["raw_json"] = json.dumps(data, ensure_ascii=False)[:8000]
                await client.report_success()
                await self.storage.upsert_position(row)
                comp = {k: row.get(k, "") for k in
                        ("company_number", "company_name", "company_size",
                         "financing_stage", "industry_name")}
                if comp.get("company_number"):
                    await self.storage.upsert_company(comp)
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
