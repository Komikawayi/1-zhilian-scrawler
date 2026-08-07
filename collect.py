# -*- coding: utf-8 -*-
"""
智联采集器 — CLI (单机异步流水线 + Redis 分布式任务队列)

三种模式:
  默认      单机异步流水线 (搜索→内存队列→详情并发→SQLite)
  --produce Redis 模式: 搜索 SSR → 唯一 number 入队 Redis (万级任务池)
  --consume Redis 模式: N 个 worker 进程并发领任务拉详情 → SQLite
  --stats   Redis 模式: 队列统计

用法:
  py collect.py --kw python,java --jl 530 --pages 5                  # 单机流水线
  py collect.py --produce --kw python,java --cities 530,538 --pages 10 --clear  # 建万级任务池
  py collect.py --consume --workers 4 --concurrency 10 --rate 20     # 4进程×10并发消费
  py collect.py --stats                                             # 队列进度
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from utils.async_client import (
    AsyncRateLimiter, AsyncZhilianClient, RedisRateLimiter,
    fetch_position_detail_v2_async,
)
from utils.parser import extract_initial_state, parse_position_detail_v2
from utils.pipeline import Pipeline, PipelineConfig
from utils.risk import RiskState
from utils.storage_pg import AsyncStorage
from utils.task_queue import TaskQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collect")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="智联采集器 (异步流水线 + Redis 分布式)")
    ap.add_argument("--kw", help="搜索关键词, 逗号分隔")
    ap.add_argument("--jl", help="城市代码 (如 530)")
    ap.add_argument("--city", help="城市中文名 (如 北京)")
    ap.add_argument("--cities", help="多城市代码, 逗号分隔 (万级扩量用, 覆盖 --jl)")
    ap.add_argument("--pages", type=int, default=5, help="每关键词页数 (默认 5)")
    ap.add_argument("--concurrency", type=int, default=settings.DETAIL_CONCURRENCY,
                    help="详情并发数 (默认 %d)" % settings.DETAIL_CONCURRENCY)
    ap.add_argument("--search-concurrency", type=int, default=settings.SEARCH_CONCURRENCY)
    ap.add_argument("--rate", type=float, default=settings.DETAIL_RATE_PER_SEC,
                    help="全局限速 请求/秒 (默认 %.1f)" % settings.DETAIL_RATE_PER_SEC)
    ap.add_argument("--db", default=settings.DB_URL, help="PostgreSQL URL (默认 %s)" % settings.DB_URL[:40] + "...")
    ap.add_argument("--export", help="采集后把 positions 导出为 CSV 路径")
    ap.add_argument("--resume", action="store_true", help="跳过已入库 number (单机流水线)")
    ap.add_argument("--name", default="", help="运行命名 (runs 表标记)")
    # Redis 分布式
    ap.add_argument("--redis", default=settings.REDIS_URL, help="Redis URL")
    ap.add_argument("--produce", action="store_true", help="Redis: 搜索→任务入队")
    ap.add_argument("--consume", action="store_true", help="Redis: 多进程消费 worker")
    ap.add_argument("--stats", action="store_true", help="Redis: 队列统计")
    ap.add_argument("--workers", type=int, default=settings.WORKERS, help="消费 worker 进程数")
    ap.add_argument("--max-attempts", type=int, default=settings.MAX_ATTEMPTS,
                    help="任务失败重试次数")
    ap.add_argument("--clear", action="store_true", help="produce 前清空任务队列")
    return ap.parse_args()


def resolve_city(args) -> str:
    city = args.jl
    if not city:
        city = settings.CITY_CODES.get(args.city or "", settings.DEFAULT_CITY)
    return city


# ====================================================================
# 默认: 单机异步流水线
# ====================================================================

async def _amain(args) -> None:
    if not args.kw:
        if args.export:
            storage = await AsyncStorage.create(args.db)
            n = await storage.export_csv(args.export, "positions")
            await storage.close()
            logger.info("已导出 %d 行 -> %s", n, args.export)
            return
        logger.error("缺少 --kw, 或用 --export 仅导出")
        sys.exit(1)
    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]
    cfg = PipelineConfig(
        keywords=keywords, city=resolve_city(args), pages=args.pages,
        detail_concurrency=args.concurrency, search_concurrency=args.search_concurrency,
        detail_rate_per_sec=args.rate, db_url=args.db, resume=args.resume, name=args.name,
    )
    logger.info("单机流水线: kw=%s city=%s pages=%d 并发=%d", keywords, cfg.city, cfg.pages, cfg.detail_concurrency)
    result = await Pipeline(cfg).run()
    logger.info("结果: %s", result)
    if args.export:
        storage = await AsyncStorage.create(args.db)
        n = await storage.export_csv(args.export, "positions")
        await storage.close()
        logger.info("已导出 %d 行 -> %s", n, args.export)


# ====================================================================
# Redis: producer (搜索 → 任务入队)
# ====================================================================

async def _search_page_async(client, kw, city, page) -> str:
    url = f"https://sou.zhaopin.com/?jl={city}&kw={kw}&p={page}"
    resp = await client.get(url, allow_redirects=True)
    if resp.status_code != 200:
        raise ConnectionError(f"HTTP {resp.status_code}")
    text = resp.text
    if "Security Verification" in text:
        await client.report_captcha()
        raise ConnectionError("EdgeOne 拦截")
    if "__INITIAL_STATE__" not in text:
        await client.report_challenge()
        raise ConnectionError("缺 __INITIAL_STATE__")
    await client.report_success()
    return text


async def _produce(args) -> int:
    queue = TaskQueue(args.redis)
    if args.clear:
        await queue.clear()
    cities = [c.strip() for c in (args.cities or args.jl).split(",") if c.strip()]
    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]
    client = AsyncZhilianClient(risk=RiskState())
    total, failed = 0, 0
    try:
        for city in cities:
            for kw in keywords:
                for page in range(1, args.pages + 1):
                    try:
                        html = await _search_page_async(client, kw, city, page)
                        state = extract_initial_state(html)
                        nums = [it.get("number") for it in (state.get("positionList") or [])
                                if isinstance(it, dict) and it.get("number")]
                        added = await queue.enqueue_many(nums)
                        total += added
                        logger.info("produce %s/%s p%d: 新增 %d (累计 %d)", kw, city, page, added, total)
                        if total > settings.TASK_POOL_CAP:
                            logger.warning("任务池达上限 %d, 停止", settings.TASK_POOL_CAP)
                            return total
                    except Exception as e:
                        failed += 1
                        logger.warning("produce %s/%s p%d 失败: %s", kw, city, page, str(e)[:80])
                    await asyncio.sleep(0.2)
    finally:
        await client.close()
        await queue.close()
    stats = await TaskQueue(args.redis).stats()
    logger.info("produce 完成: 新增 %d, 队列 %s", total, stats)
    return total


# ====================================================================
# Redis: consumer (多进程 worker)
# ====================================================================

@dataclass
class WorkerConfig:
    redis_url: str
    db_url: str
    concurrency: int = 10
    rate_per_sec: float = 8.0
    max_attempts: int = 3
    worker_id: int = 0


async def _consume_loop(client, queue, storage, cfg: WorkerConfig) -> Dict:
    """单个消费协程: 取任务→拉详情→入库。队列耗尽后退出。"""
    ok, fail = 0, 0
    started = time.time()
    while True:
        num = await queue.dequeue(timeout=1)
        if num is None:
            if await queue.is_drained():
                return {"ok": ok, "fail": fail}
            continue
        try:
            data = await fetch_position_detail_v2_async(client, num)
            row = parse_position_detail_v2(data)
            row["position_number"] = num
            row["source"] = "detailv2"
            row["raw_json"] = json.dumps(data, ensure_ascii=False)[:8000]
            await storage.upsert_position(row)
            comp = {k: row.get(k, "") for k in
                    ("company_number", "company_name", "company_size",
                     "financing_stage", "industry_name")}
            if comp.get("company_number"):
                await storage.upsert_company(comp)
            await queue.complete(num)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            await queue.fail(num, max_attempts=cfg.max_attempts)
        done = ok + fail
        if done % 100 == 0:
            el = time.time() - started
            logger.info("  worker%d CHECKPOINT %d: 成功%d 失败%d 耗时%.0fs (%.1f/s)",
                        cfg.worker_id, done, ok, fail, el, done / el if el else 0)


async def _worker_loop(cfg: WorkerConfig) -> Dict:
    """单个 worker 进程的 asyncio 循环 (并发 detail 协程)。"""
    queue = TaskQueue(cfg.redis_url)
    rate_limiter = RedisRateLimiter(queue.redis, cfg.rate_per_sec)
    risk = RiskState()
    client = AsyncZhilianClient(risk=risk, rate_limiter=rate_limiter)
    storage = await AsyncStorage.create(cfg.db_url)
    try:
        coros = [_consume_loop(client, queue, storage, cfg) for _ in range(cfg.concurrency)]
        results = await asyncio.gather(*coros)
    finally:
        await client.close()
        await storage.close()
        await queue.close()
    ok = sum(r["ok"] for r in results)
    fail = sum(r["fail"] for r in results)
    logger.info("worker%d 完成: 成功%d 失败%d", cfg.worker_id, ok, fail)
    return {"ok": ok, "fail": fail}


def _worker_main(cfg: WorkerConfig) -> None:
    """进程入口 (multiprocessing spawn 需要顶层可导入)。"""
    asyncio.run(_worker_loop(cfg))


def _consume(args) -> None:
    workers = max(1, args.workers)
    cfg_list = [WorkerConfig(redis_url=args.redis, db_url=args.db,
                             concurrency=args.concurrency, rate_per_sec=args.rate,
                             max_attempts=args.max_attempts, worker_id=i)
                for i in range(workers)]
    logger.info("启动 %d 个 worker 进程 (每进程 %d 并发, 全局限速 %.1f/s, db=%s)",
                workers, args.concurrency, args.rate, args.db)
    procs = [multiprocessing.Process(target=_worker_main, args=(cfg,)) for cfg in cfg_list]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


async def _stats(args) -> None:
    queue = TaskQueue(args.redis)
    s = await queue.stats()
    await queue.close()
    logger.info("Redis 队列: %s", s)
    print(json.dumps(s, ensure_ascii=False, indent=2))


# ====================================================================
# main
# ====================================================================

def main() -> None:
    args = parse_args()
    try:
        if args.stats:
            asyncio.run(_stats(args))
        elif args.produce:
            total = asyncio.run(_produce(args))
            logger.info("任务池已建: %d 个任务", total)
        elif args.consume:
            _consume(args)
        else:
            asyncio.run(_amain(args))
    except KeyboardInterrupt:
        logger.warning("收到中断, 已优雅退出")


if __name__ == "__main__":
    main()
