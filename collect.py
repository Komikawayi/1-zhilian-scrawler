# -*- coding: utf-8 -*-
"""
智联采集器 — CLI (Redis 分布式任务队列, 搜索/详情双队列)

任务流:
  --produce   生成任务 (城市×关键词×页码 → 搜索队列 keyword: 任务;
                          --companies 直接生成 company: 任务)
  --consume   多进程 worker 消费:
                keyword:任务 → sou 搜索 → 岗位号入详情队列 + 公司入 companies 表
                company:任务  → 公司名搜索(全国) → 该公司全部岗位号入详情队列
                position:任务 → position-detailv2 → upsert PostgreSQL
  --stats     队列统计

搜索与详情共享同一 Redis 全局限速 (15/s, 同一 IP 信誉资源);
风控状态机跨 run 持久化 (utils/risk.py)。

用法:
  py collect.py --produce --kw smt,pcba --cities 653,530        # 建搜索任务池 (自动翻完所有页)
  py collect.py --produce --companies CZ883210900,CZ1425835260            # 公司名补采任务
  py collect.py --consume --workers 4 --concurrency 10 --rate 15          # 多进程消费
  py collect.py --stats                                                   # 队列进度
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
from dataclasses import dataclass
from typing import Dict, List
from urllib.parse import quote

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
from utils.task_queue import (
    QUEUE_POSITION, QUEUE_SEARCH, TASK_COMPANY, TASK_KEYWORD, TASK_POSITION,
    TaskQueue,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collect")

# 搜索 worker 空闲安全阀: 详情 worker 等这么久没新任务就退出 (防搜索全崩后永等)
IDLE_EXIT_SEC = 600

# raw_json 截断: 单字段上限 + 整体预算 (先截字段再序列化, 保证 jsonb 合法)
RAW_JSON_MAX = 8000          # 单字段截断上限 (字符)
RAW_JSON_BUDGET = 12000      # 整体序列化预算 (防多字段叠加膨胀)


def _trim_raw_json(data: dict) -> str:
    """截断超长字段后整体序列化 (保证合法 JSON), 且整体不超过预算。

    原实现 json.dumps(data)[:8000] 会在字符串中间切断 (如长 jobDesc 含引号),
    产生非法 JSON → asyncpg InvalidTextRepresentationError。改为先截字段再 dumps;
    若整体仍超预算, 再按"最短字段优先放弃"裁剪。
    """
    def trim(v, budget=RAW_JSON_MAX):
        if isinstance(v, str) and len(v) > budget:
            return v[:budget]
        if isinstance(v, dict):
            return {k: trim(x, budget) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [trim(x, budget) for x in v]
        return v
    trimmed = trim(data)
    raw = json.dumps(trimmed, ensure_ascii=False)
    if len(raw) <= RAW_JSON_BUDGET:
        return raw
    # 整体超预算: 缩短单字段上限重试 (半减直到达标)
    budget = RAW_JSON_MAX // 2
    while len(raw) > RAW_JSON_BUDGET and budget >= 100:
        trimmed = trim(data, budget)
        raw = json.dumps(trimmed, ensure_ascii=False)
        budget //= 2
    return raw


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="智联采集器 (Redis 分布式: 搜索/详情双队列)")
    # 任务生成
    ap.add_argument("--produce", action="store_true", help="生成任务 (关键词/公司)")
    ap.add_argument("--kw", help="搜索关键词, 逗号分隔")
    ap.add_argument("--cities", help="多城市代码, 逗号分隔 (如 653,530)")
    ap.add_argument("--jl", help="单城市代码 (兼容)")
    ap.add_argument("--pages", type=int, default=5,
                    help="仅 --single 单机模式: 每关键词页数 (分布式自动翻完所有页)")
    ap.add_argument("--companies", help="公司号列表, 逗号分隔 (生成 company: 任务)")
    ap.add_argument("--clear", action="store_true", help="produce 前清空对应队列")
    # 消费
    ap.add_argument("--consume", action="store_true", help="多进程消费 worker")
    ap.add_argument("--single", action="store_true", help="单机流水线 (调试用, 非分布式)")
    ap.add_argument("--workers", type=int, default=settings.WORKERS, help="worker 进程数")
    ap.add_argument("--concurrency", type=int, default=settings.DETAIL_CONCURRENCY,
                    help="每进程并发协程数")
    ap.add_argument("--rate", type=float, default=settings.DETAIL_RATE_PER_SEC,
                    help="全局限速 请求/秒 (搜索+详情共享)")
    ap.add_argument("--max-attempts", type=int, default=settings.MAX_ATTEMPTS,
                    help="任务失败重试次数")
    # 环境
    ap.add_argument("--redis", default=settings.REDIS_URL, help="Redis URL")
    ap.add_argument("--db", default=settings.DB_URL, help="PostgreSQL URL")
    ap.add_argument("--stats", action="store_true", help="队列统计")
    ap.add_argument("--name", default="", help="运行命名 (runs 表标记)")
    return ap.parse_args()


# ====================================================================
# produce: 生成任务 (不直接发搜索请求)
# ====================================================================

async def _produce(args) -> int:
    search_q = TaskQueue(args.redis, queue_type=QUEUE_SEARCH)
    if args.clear:
        await search_q.clear()
    total = 0
    try:
        # 1) 关键词任务: 城市×关键词 笛卡尔积 (每组合 1 个任务, 消费时自动翻完所有页)
        if args.kw:
            cities = [c.strip() for c in (args.cities or args.jl or "0").split(",") if c.strip()]
            keywords = [k.strip() for k in args.kw.split(",") if k.strip()]
            for city in cities:
                for kw in keywords:
                    tid = search_q.make_keyword_task(city, kw)
                    if await search_q.enqueue(tid):
                        total += 1
            logger.info("关键词任务已生成: %d 城市 × %d 关键词 = %d 个 (每任务自动翻完所有页)",
                        len(cities), len(keywords), total)
        # 2) 公司任务
        if args.companies:
            companies = [c.strip() for c in args.companies.split(",") if c.strip()]
            for num in companies:
                tid = search_q.make_company_task(num)
                if await search_q.enqueue(tid):
                    total += 1
            logger.info("公司任务已生成: %d 个", len(companies))
        if total == 0:
            logger.warning("produce 未生成任何任务 (--kw 或 --companies 至少给一个)")
    finally:
        await search_q.close()
    return total


# ====================================================================
# consume: 多进程 worker (搜索 worker + 详情 worker 同进程池, 按前缀分发)
# ====================================================================

@dataclass
class WorkerConfig:
    redis_url: str
    db_url: str
    concurrency: int = 10
    rate_per_sec: float = 15.0
    max_attempts: int = 3
    worker_id: int = 0


# ---- 搜索任务消费 (keyword:/company:) ----

async def _search_page_async(client, city, kw, page) -> str:
    """搜索页 (sou SSR); city='0' 或不带 = 全国。

    kw 中文 (公司名/关键词) 必须 URL 编码, curl_cffi 不自动编码手拼 query。
    """
    q = quote(kw)
    url = f"https://sou.zhaopin.com/?jl={city}&kw={q}&p={page}" if city != "0" else \
          f"https://sou.zhaopin.com/?kw={q}&p={page}"
    resp = await client.get(url, allow_redirects=True)
    if resp.status_code != 200:
        raise ConnectionError(f"HTTP {resp.status_code}")
    text = resp.text
    if "Security Verification" in text:
        await client.report_captcha()
        raise ConnectionError("EdgeOne 拦截 (验证码)")
    if "__INITIAL_STATE__" not in text:
        await client.report_challenge()
        raise ConnectionError("缺 __INITIAL_STATE__")
    await client.report_success()
    return text


async def _consume_keyword_task(queue, client, storage, pos_queue, task_id: str,
                                max_pages: int = 100) -> None:
    """keyword:{city}:{kw} → 搜索(自动翻完所有页) → 岗位号入详情队列 + 公司号入搜索队列。

    分页终止: 用 SSR 的 pages 字段 (总页数), 翻完为止; SSR 无 pages 时以连续空页兜底。
    """
    _, city, kw = task_id.split(":", 2)
    added = 0
    matched_pages = 0   # 连续空页计数 (SSR 无 pages 时兜底终止)
    for page in range(1, max_pages + 1):
        html = await _search_page_async(client, city, kw, page)
        state = extract_initial_state(html)
        pl = state.get("positionList") or []
        total_pages = int(state.get("pages") or 0)
        nums = [it.get("number") for it in pl if isinstance(it, dict) and it.get("number")]
        if nums:
            added += await pos_queue.enqueue_many(
                pos_queue.make_position_task(n) for n in nums)
        # 公司号+名 → companies 表 (mini), 并生成 company: 补采任务
        for it in pl:
            if not isinstance(it, dict):
                continue
            cn, name = it.get("companyNumber"), it.get("companyName")
            if cn and name:
                await storage.upsert_company_mini(cn, name)
                comp_task = queue.make_company_task(cn)
                if not await queue.is_failed(comp_task):   # 已重试超限不再拉起 (防失败风暴)
                    await queue.enqueue(comp_task)
        # 精确终止: SSR 给出总页数, 翻完为止
        if total_pages > 0 and page >= total_pages:
            break
        # 兜底终止: SSR 无 pages 时, 连续空页 2 次或整页空
        if total_pages == 0:
            if not pl:
                break
            matched_pages = matched_pages + 1 if not nums else 0
            if matched_pages >= 2:
                break
    logger.debug("keyword %s/%s: 翻 %d 页, %d 岗位入队", kw, city, page, added)


async def _consume_company_task(queue, client, storage, pos_queue, task_id: str,
                                max_pages: int = 50) -> None:
    """company:{number} → 公司名搜索(全国) → 该公司全部岗位号入详情队列。

    严格过滤 company_number: 公司名可能命中同名公司, 只收目标公司的岗位。
    分页终止: 用 SSR 的 pages 字段 (总页数), 精确翻完。
    """
    _, company_number = task_id.split(":", 1)
    company_name = await storage.get_company_name(company_number)
    if not company_name:
        logger.warning("公司 %s 无名称 (companies 表缺 mini 记录), 跳过", company_number)
        return
    added = 0
    matched_pages = 0   # 连续"无目标公司岗位"页计数 (SSR 无 pages 时兜底终止)
    for page in range(1, max_pages + 1):
        html = await _search_page_async(client, "0", company_name, page)
        state = extract_initial_state(html)
        pl = state.get("positionList") or []
        total_pages = int(state.get("pages") or 0)
        page_matched = 0
        for it in pl:
            if not isinstance(it, dict):
                continue
            if it.get("companyNumber") != company_number:
                continue  # 同名公司, 严格过滤
            num = it.get("number")
            if num and await pos_queue.enqueue(pos_queue.make_position_task(num)):
                added += 1
            page_matched += 1
        # 精确终止: SSR 明确给出总页数, 翻完为止
        if total_pages > 0 and page >= total_pages:
            break
        # 兜底终止: SSR 无 pages 字段时, 以"整页空"或"连续空页"为结束
        if total_pages == 0:
            if not pl or not page_matched:
                matched_pages += 1
                if matched_pages >= 2 or not pl:
                    break
            else:
                matched_pages = 0
        elif not pl:
            break  # 有 pages 但提前空页, 防御
    logger.debug("company %s: 新增 %d 岗位入详情队列", company_number, added)


async def _consume_search_loop(queue, client, storage, pos_queue,
                               cfg: WorkerConfig) -> Dict:
    """搜索队列消费协程: keyword:/company: 任务 → 产出详情任务。"""
    ok, fail = 0, 0
    idle_since = time.time()
    while True:
        task_id = await queue.dequeue(timeout=1)
        if task_id is None:
            if await queue.is_drained() and await pos_queue.is_drained():
                return {"ok": ok, "fail": fail}
            if time.time() - idle_since > IDLE_EXIT_SEC:
                logger.info("worker%d 搜索空闲 %ds 无新任务, 退出", cfg.worker_id, IDLE_EXIT_SEC)
                return {"ok": ok, "fail": fail}
            continue
        idle_since = time.time()
        try:
            if task_id.startswith(TASK_KEYWORD + ":"):
                await _consume_keyword_task(queue, client, storage, pos_queue, task_id)
            elif task_id.startswith(TASK_COMPANY + ":"):
                await _consume_company_task(queue, client, storage, pos_queue, task_id)
            else:
                raise ValueError(f"未知搜索任务类型: {task_id}")
            await queue.complete(task_id)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            logger.warning("搜索任务 %s 失败: %s", task_id, str(e)[:100])
            await queue.fail(task_id, max_attempts=cfg.max_attempts)


# ---- 详情任务消费 (position:) ----

async def _consume_position_loop(queue, client, storage, cfg: WorkerConfig,
                                 search_q=None) -> Dict:
    """详情队列消费协程: position:任务 → detailv2 → upsert PG。

    退出条件 (防假早退): 详情队列空**且**搜索池整体静默 (搜索队列空 + 无在途
    搜索任务) 才立即返回; 否则等 IDLE_EXIT_SEC 空闲超时。搜索 worker 还在
    产出时详情 worker 不提前退出。
    """
    ok, fail = 0, 0
    idle_since = time.time()
    while True:
        task_id = await queue.dequeue(timeout=1)
        if task_id is None:
            if await queue.is_drained() and (search_q is None or await search_q.is_drained()):
                return {"ok": ok, "fail": fail}
            if time.time() - idle_since > IDLE_EXIT_SEC:
                logger.info("worker%d 详情空闲 %ds 无新任务, 退出", cfg.worker_id, IDLE_EXIT_SEC)
                return {"ok": ok, "fail": fail}
            continue
        idle_since = time.time()
        number = task_id.split(":", 1)[1]
        try:
            data = await fetch_position_detail_v2_async(client, number)
            row = parse_position_detail_v2(data)
            row["position_number"] = number
            row["source"] = "detailv2"
            row["raw_json"] = _trim_raw_json(data)   # 截断长字段后整体序列化 (保证合法 JSON)
            await storage.upsert_position(row)
            comp = {k: row.get(k, "") for k in
                    ("company_number", "company_name", "company_size",
                     "financing_stage", "industry_name")}
            if comp.get("company_number"):
                await storage.upsert_company(comp)
            await queue.complete(task_id)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            logger.warning("详情 %s 失败: %s", number, str(e)[:100])
            await queue.fail(task_id, max_attempts=cfg.max_attempts)
        done = ok + fail
        if done % 100 == 0:
            logger.info("worker%d 详情 CHECKPOINT %d: 成功%d 失败%d",
                        cfg.worker_id, done, ok, fail)


# ---- worker 进程 ----

async def _watchdog(search_q, pos_q, interval: float = 30.0, max_age: int = 120) -> None:
    """崩溃回收 watchdog: 周期性回收两个队列的超时在途任务 (分布式锁防并发)。"""
    try:
        while True:
            await asyncio.sleep(interval)
            await search_q.recover_stale(max_age=max_age)
            await pos_q.recover_stale(max_age=max_age)
    except asyncio.CancelledError:
        pass


async def _worker_loop(cfg: WorkerConfig) -> Dict:
    """单个 worker 进程: 搜索消费 + 详情消费 + 崩溃 watchdog, 共享全局限速。"""
    search_q = TaskQueue(cfg.redis_url, queue_type=QUEUE_SEARCH)
    pos_q = TaskQueue(cfg.redis_url, queue_type=QUEUE_POSITION)
    rate_limiter = RedisRateLimiter(search_q.redis, cfg.rate_per_sec)
    risk = RiskState()
    client = AsyncZhilianClient(risk=risk, rate_limiter=rate_limiter)
    storage = await AsyncStorage.create(cfg.db_url)
    # watchdog 独立 Task: 消费 loop 全退后 cancel, 否则 gather 永不返回 (流程不收敛)
    watchdog = asyncio.create_task(_watchdog(search_q, pos_q))
    coros = [
        _consume_search_loop(search_q, client, storage, pos_q, cfg)
        for _ in range(max(1, cfg.concurrency // 2))
    ]
    coros += [
        _consume_position_loop(pos_q, client, storage, cfg, search_q=search_q)
        for _ in range(cfg.concurrency)
    ]
    try:
        results = await asyncio.gather(*coros)
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await client.close()
        await storage.close()
        await search_q.close()
        await pos_q.close()
    ok = sum(r.get("ok", 0) for r in results if isinstance(r, dict))
    fail = sum(r.get("fail", 0) for r in results if isinstance(r, dict))
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
    search_q = TaskQueue(args.redis, queue_type=QUEUE_SEARCH)
    pos_q = TaskQueue(args.redis, queue_type=QUEUE_POSITION)
    try:
        ss = await search_q.stats()
        ps = await pos_q.stats()
    finally:
        await search_q.close()
        await pos_q.close()
    print(json.dumps({"search": ss, "position": ps}, ensure_ascii=False, indent=2))


# ====================================================================
# single: 单机流水线 (调试用; 生产走 Redis 分布式)
# ====================================================================

async def _amain(args) -> None:
    """单机流水线: 多城市循环, 每城市跑 Pipeline (搜索→队列→详情→PG)。"""
    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]
    cities = [c.strip() for c in (args.cities or args.jl or "0").split(",") if c.strip()]
    total_success = total_fail = 0
    for city in cities:
        cfg = PipelineConfig(
            keywords=keywords, city=city, pages=args.pages,
            detail_concurrency=args.concurrency,
            detail_rate_per_sec=args.rate, db_url=args.db, name=args.name,
        )
        logger.info("单机流水线: kw=%s city=%s pages=%d 并发=%d",
                    keywords, city, cfg.pages, cfg.detail_concurrency)
        result = await Pipeline(cfg).run()
        total_success += result.get("success", 0)
        total_fail += result.get("fail", 0)
    logger.info("单机流水线完成: 成功%d 失败%d", total_success, total_fail)


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
        elif args.single:
            if not args.kw:
                logger.error("--single 需要 --kw")
                sys.exit(1)
            asyncio.run(_amain(args))
        else:
            logger.error("请指定 --produce / --consume / --stats / --single 之一")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("收到中断, 已优雅退出")


if __name__ == "__main__":
    main()
