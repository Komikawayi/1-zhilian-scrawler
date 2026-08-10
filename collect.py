# -*- coding: utf-8 -*-
"""
智联采集器 — CLI (Redis 分布式任务队列, 搜索/详情双队列)

任务流:
  --produce   生成任务 (城市×关键词 → 搜索队列 keyword: 任务, 消费自动翻页;
                          --companies 直接生成 company: 任务)
  --consume   多进程 worker 消费:
                keyword:任务 → sou 搜索 → 岗位号入详情队列 + 公司入 companies 表
                company:任务  → 公司名搜索(全国) → 该公司全部岗位号入详情队列
                position:任务 → position-detailv2 → upsert PostgreSQL
  --stats     队列统计

搜索/详情各自独立 Redis 滑动窗口限速桶 (默认 搜索 100/s + 详情 400/s,
搜索仍受共享风控状态和限速桶约束, 详情无 IP 信誉依赖可高频);
风控状态机通过 Redis 按出口身份跨 worker/run 共享 (utils/risk.py)。

用法:
  py collect.py --produce --kw smt,pcba --cities 653,530        # 建搜索任务池 (自动翻完所有页)
  py collect.py --produce --companies CZ883210900,CZ1425835260            # 公司名补采任务
  py collect.py --consume --workers 2 --concurrency 400 --search-concurrency 100 --search-rate 100 --detail-rate 400  # 多进程消费
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
from typing import Dict
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from utils.async_client import (
    AsyncZhilianClient, PositionUnavailableError, RedisRateLimiter,
    fetch_position_detail_v2_async,
)
from utils.latency_stats import LatencyStats
from utils.parser import extract_initial_state, parse_position_detail_v2
from utils.pipeline import Pipeline, PipelineConfig
from utils.risk import AsyncRedisRiskState
from utils.storage_pg import AsyncStorage
from utils.task_queue import (
    QUEUE_POSITION, QUEUE_SEARCH, TASK_COMPANY, TASK_KEYWORD,
    TaskQueue,
)

def _setup_logging() -> str:
    """日志双输出: 控制台只显示 WARNING+ (终端干净, 进度交给进度显示器),
    文件保留 INFO 全量 (output/logs/collect-<ts>-<pid>.log, gitignore)。

    每进程独立文件 (pid 后缀): multiprocessing spawn 子进程各自重新 import 本模块,
    若共享同一文件会多进程并发写导致行交错; 按 pid 分文件无竞争且可回溯。
    """
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"collect-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)          # 终端只留 错误/警告 (UI 交给进度显示器)
    console.setFormatter(fmt)
    file_h = logging.FileHandler(path, encoding="utf-8")
    file_h.setLevel(logging.INFO)              # 文件保留完整 INFO, 便于回溯排查
    file_h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_h])
    return path


_LOG_FILE = _setup_logging()   # 模块级: 主进程 + 各 spawn worker 进程各建一份
logger = logging.getLogger("collect")

# 搜索 worker 空闲安全阀: 详情 worker 等这么久没新任务就退出 (防搜索全崩后永等)
IDLE_EXIT_SEC = 600


def _fmt_elapsed(seconds: float) -> str:
    """秒 → HH:MM:SS (任务总耗时显示)。"""
    hh, mm, ss = int(seconds // 3600), int(seconds % 3600 // 60), int(seconds % 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"

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
                    help="详情并发协程数/worker")
    ap.add_argument("--search-concurrency", type=int, default=settings.SEARCH_CONCURRENCY,
                    help="搜索并发协程数/worker (默认 100)")
    ap.add_argument("--rate", type=float, default=settings.DETAIL_RATE_PER_SEC,
                    help="[兼容] 全局旧限速; 用 --search-rate/--detail-rate 分桶")
    ap.add_argument("--search-rate", type=float, default=settings.SEARCH_RATE_PER_SEC,
                    help="搜索桶限速 req/s (IP 信誉敏感)")
    ap.add_argument("--detail-rate", type=float, default=settings.DETAIL_RATE_PER_SEC,
                    help="详情桶限速 req/s (提高前需持续压测)")
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
            print(f"关键词任务已生成: {len(cities)} 城市 × {len(keywords)} 关键词 "
                  f"= {total} 个 (每任务自动翻完所有页)")
        # 2) 公司任务
        if args.companies:
            companies = [c.strip() for c in args.companies.split(",") if c.strip()]
            for num in companies:
                tid = search_q.make_company_task(num)
                if await search_q.enqueue(tid):
                    total += 1
            print(f"公司任务已生成: {len(companies)} 个")
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
    concurrency: int = settings.DETAIL_CONCURRENCY
    search_concurrency: int = settings.SEARCH_CONCURRENCY
    search_rate: float = settings.SEARCH_RATE_PER_SEC
    detail_rate: float = settings.DETAIL_RATE_PER_SEC
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
                                stats: LatencyStats = None,
                                max_pages: int = 100) -> None:
    """keyword:{city}:{kw} → 搜索(自动翻完所有页) → 岗位号入详情队列 + 公司号入搜索队列。

    分页终止: 用 SSR 的 pages 字段 (总页数), 翻完为止; SSR 无 pages 时以连续空页兜底。
    """
    _, city, kw = task_id.split(":", 2)
    added = 0
    matched_pages = 0   # 连续空页计数 (SSR 无 pages 时兜底终止)
    for page in range(1, max_pages + 1):
        await queue.heartbeat(task_id)
        html = await _search_page_async(client, city, kw, page)
        t0 = time.perf_counter()
        state = extract_initial_state(html)
        if stats is not None:
            stats.record("search_parse", time.perf_counter() - t0)
        pl = state.get("positionList") or []
        total_pages = int(state.get("pages") or 0)
        nums = [it.get("number") for it in pl if isinstance(it, dict) and it.get("number")]
        comps = [(it.get("companyNumber"), it.get("companyName"))
                 for it in pl if isinstance(it, dict)]
        comps = [c for c in comps if c[0] and c[1]]
        if nums:
            # 批量: 岗位入详情队列 (按 seen 去重) + 公司号入搜索队列 (去重+跳过 failed)
            #  + 公司名落库, 各 1 次 RTT (原来每岗位/公司 ~2-3 次, 页级后处理 ~100ms→~10ms)
            added += await pos_queue.enqueue_many_batch(
                pos_queue.make_position_task(n) for n in nums)
        if comps:
            await queue.enqueue_new_not_failed(
                queue.make_company_task(c[0]) for c in comps)
        # 公司号+名 → companies 表 (mini), 计 db_search (页级聚合, 批量 1 条 SQL)
        t0 = time.perf_counter()
        if comps:
            await storage.upsert_company_mini_batch(comps)
        if stats is not None:
            stats.record("db_search", time.perf_counter() - t0)
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
                                stats: LatencyStats = None,
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
        await queue.heartbeat(task_id)
        html = await _search_page_async(client, "0", company_name, page)
        t0 = time.perf_counter()
        state = extract_initial_state(html)
        if stats is not None:
            stats.record("search_parse", time.perf_counter() - t0)
        pl = state.get("positionList") or []
        total_pages = int(state.get("pages") or 0)
        matched = [it.get("number")
                   for it in pl if isinstance(it, dict)
                   and it.get("companyNumber") == company_number]
        if matched:
            # 批量入队 (去重, 1 次 RTT; 原来逐条 ~2 次/岗位)
            added += await pos_queue.enqueue_many_batch(
                pos_queue.make_position_task(n) for n in matched)
        page_matched = len(matched)
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
                               cfg: WorkerConfig, stats: LatencyStats = None) -> Dict:
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
                await _consume_keyword_task(queue, client, storage, pos_queue,
                                            task_id, stats=stats)
                # 记录最近消费 (进度显示器读)
                await queue.redis.set("zhaopin:last_consume:search", f"关键词 {task_id}",
                                      ex=60)
            elif task_id.startswith(TASK_COMPANY + ":"):
                await _consume_company_task(queue, client, storage, pos_queue,
                                            task_id, stats=stats)
                await queue.redis.set("zhaopin:last_consume:search",
                                      f"公司补采 {task_id}", ex=60)
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
                                 search_q=None, stats: LatencyStats = None) -> Dict:
    """详情队列消费协程: position:任务 → detailv2 → upsert PG。

    退出条件 (防假早退): 详情队列空**且**搜索池整体静默 (搜索队列空 + 无在途
    搜索任务) 才立即返回; 搜索池未耗尽时详情 worker 永不因空闲退出 (新岗位可能
    零星入队, 空窗期不退避免漏采), 只有搜索池也耗尽后才受 IDLE_EXIT_SEC 约束。
    """
    ok, fail = 0, 0
    idle_since = time.time()
    while True:
        task_id = await queue.dequeue(timeout=1)
        if task_id is None:
            if await queue.is_drained() and (search_q is None or await search_q.is_drained()):
                return {"ok": ok, "fail": fail}
            # 搜索池未耗尽 (队列有任务或搜索在途): 详情 worker 永不因空闲退出,
            # 新岗位随到随采 (seen 去重下新岗位可能零星入队, 不能因为 10 分钟
            # 空窗退出而漏采)。只有搜索池也耗尽时才受 IDLE_EXIT_SEC 约束。
            if search_q is not None and not await search_q.is_drained():
                continue
            if time.time() - idle_since > IDLE_EXIT_SEC:
                logger.info("worker%d 详情空闲 %ds 无新任务, 退出", cfg.worker_id, IDLE_EXIT_SEC)
                return {"ok": ok, "fail": fail}
            continue
        idle_since = time.time()
        number = task_id.split(":", 1)[1] if ":" in task_id else task_id
        try:
            data = await fetch_position_detail_v2_async(client, number)
            t0 = time.perf_counter()
            row = parse_position_detail_v2(data)
            if stats is not None:
                stats.record("detail_parse", time.perf_counter() - t0)
            row["position_number"] = number
            row["source"] = "detailv2"
            row["raw_json"] = _trim_raw_json(data)   # 截断长字段后整体序列化 (保证合法 JSON)
            t0 = time.perf_counter()
            await storage.upsert_position(row)
            comp = {k: row.get(k, "") for k in
                    ("company_number", "company_name", "company_size",
                     "financing_stage", "industry_name")}
            if comp.get("company_number"):
                await storage.upsert_company(comp)
            if stats is not None:
                stats.record("db_detail", time.perf_counter() - t0)
            await queue.complete(task_id)
            ok += 1
            # 记录最近消费 (进度显示器读, 实时明细)
            await queue.redis.set(
                "zhaopin:last_consume:detail",
                f"{row.get('position_name', '')[:18]}|{row.get('work_city', '')}",
                ex=60)
        except PositionUnavailableError:
            # 高并发下观察到有效岗位偶发 211 后恢复 200，按普通任务有限重试；
            # 真正下架的岗位达到 max_attempts 后仍会进入 failed。
            fail += 1
            logger.info("详情 %s 暂不可用(211), 有限重试", number)
            await queue.fail(task_id, max_attempts=cfg.max_attempts)
        except Exception as e:  # noqa: BLE001
            fail += 1
            # 终端默认只留 WARNING, 偶发 curl 断连重试会刷屏 → 只记 ERROR (final 失败)
            logger.error("详情 %s 失败: %s", number, str(e)[:100])
            await queue.fail(task_id, max_attempts=cfg.max_attempts)


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


def _enable_windows_vt() -> bool:
    """Windows conhost 开启 VT 处理 (ANSI 光标/颜色解析)。返回是否成功启用。

    colorama 同款做法: SetConsoleMode 设 ENABLE_VIRTUAL_TERMINAL_PROCESSING。
    非 Windows / 非控制台 (管道, PyCharm Run 窗口) 返回 False → 走单行 \r 降级。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h_out = k32.GetStdHandle(-11)           # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h_out, ctypes.byref(mode)):
            k32.SetConsoleMode(h_out, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _progress_monitor(search_q, pos_q, cfg: WorkerConfig, risk=None,
                            stats: LatencyStats = None,
                            interval: float = 1.0) -> None:
    """实时进度显示器: 每秒刷新 4 行 (进度/消费/运行/延迟), ANSI \\x1b[4A 覆盖。

    仅 worker0 显示 (多进程 print 会行竞争); 仅当 stdout 为 tty 且未禁用时启用
    ANSI 刷新 (重定向/管道/不支持 ANSI 的终端会逐行追加成长流水线, 用
    ZHAOPIN_PROGRESS=0 强制关闭, =1 强制开启, 默认自动检测 isatty)。
    完成/失败显示**本次运行增量**: Redis done/failed 是历史累积 SCARD,
    启动时记录基线, 增量 = 当前 - 基线 (不含之前几万条历史)。
    行4: 分环节延迟 (累计均值, 本 worker 统计代表整体; 见 utils/latency_stats.py)。

    降级分支 (非 ANSI): 每秒 \r 回行首覆盖同一行 → 实时更新且不刷屏,
    仅占终端一行, 其余空间留给 WARNING 日志。适合 PyCharm/捕获环境。
    """
    _env = os.environ.get("ZHAOPIN_PROGRESS", "").lower()
    if _env == "0":
        use_ansi = False
    elif _env == "1":
        use_ansi = True
    elif os.environ.get("PYCHARM_HOSTED") or not sys.stdout.isatty():
        use_ansi = False          # PyCharm Run 窗口 / 重定向 → 单行 \r 降级 (不依赖 ANSI)
    else:
        # Unix/Git Bash/PyCharm Terminal 带 TERM; Windows conhost 尝试启用 VT → 4 行覆盖
        term = os.environ.get("TERM") or ""
        use_ansi = (bool(term) and term != "dumb") or _enable_windows_vt()
    prev_s_done = prev_d_done = None       # 上次刷新 done (滚动, 算速率)
    base_s_done = base_d_done = None       # 本次运行基线 (增量起点)
    base_s_fail = base_d_fail = 0
    prev_t = time.time()
    started = time.time()
    printed = False
    try:
        while True:
            await asyncio.sleep(interval)
            ss = await search_q.stats()
            ps = await pos_q.stats()
            # 最近消费明细 (各 worker 写入的 Redis key)
            last_s = await search_q.redis.get("zhaopin:last_consume:search") or ""
            last_d = await search_q.redis.get("zhaopin:last_consume:detail") or ""
            now = time.time()
            # 首循环: 记录本次运行基线 (Redis done/failed 是历史累积, 增量=当前-基线)
            if base_s_done is None:
                base_s_done, base_d_done = ss["done"], ps["done"]
                base_s_fail, base_d_fail = ss["failed"], ps["failed"]
                prev_s_done, prev_d_done = base_s_done, base_d_done
                prev_t = now
                continue
            dt = now - prev_t
            s_done = ss["done"] - base_s_done     # 本次运行累计完成 (不含历史)
            d_done = ps["done"] - base_d_done
            s_fail = ss["failed"] - base_s_fail
            d_fail = ps["failed"] - base_d_fail
            s_rate = (ss["done"] - prev_s_done) / dt if dt > 0 else 0
            d_rate = (ps["done"] - prev_d_done) / dt if dt > 0 else 0
            prev_s_done, prev_d_done = ss["done"], ps["done"]
            prev_t = now
            if cfg.worker_id == 0 and use_ansi:
                # 行1: 进度 (搜索/详情双桶独立速率, 本次运行增量)
                line1 = (f"\r[搜索] 排队{ss['queue']:>5} 处理中{ss['processing']:>3} "
                         f"完成{s_done:>6} 失败{s_fail:>2} | {s_rate:5.1f}/s | "
                         f"[详情] 排队{ps['queue']:>6} 处理中{ps['processing']:>3} "
                         f"完成{d_done:>6} 失败{d_fail:>2} | {d_rate:5.1f}/s   ")
                # 行2: 消费明细
                line2 = (f"\r[消费] 搜索:{last_s[:28]:30s} | 详情:{last_d[:32]:34s}")
                # 行3: 运行时间 + 汇总 (本次增量)
                elapsed = now - started
                hh, mm, ssec = int(elapsed // 3600), int(elapsed % 3600 // 60), int(elapsed % 60)
                total_done = s_done + d_done
                total_fail = s_fail + d_fail
                total_rate = total_done / elapsed if elapsed > 0 else 0
                rk = getattr(risk, "state", "") or "" if risk else ""
                line3 = (f"\r[运行] {hh:02d}:{mm:02d}:{ssec:02d}  "
                         f"总完成 {total_done:>7} 总失败 {total_fail:>4}  "
                         f"综合 {total_rate:6.1f}/s  风控:{rk or 'ok'}")
                # 行4: 分环节延迟 (累计均值, 平滑; 本地解析/DB 显示证明非瓶颈)
                def _ms(stage: str, nd: int = 0) -> str:
                    v = stats.current_mean(stage) if stats else None
                    return f"{v:.{nd}f}" if v is not None else "-"
                line4 = (f"\r[延迟] 搜索 {_ms('search_net'):>4}ms(TTFB {_ms('search_ttfb'):>3}) | "
                         f"详情 {_ms('detail_net'):>4}ms(TTFB {_ms('detail_ttfb'):>3}) | "
                         f"解析 {_ms('detail_parse', 1):>4}ms | 入库 {_ms('db_detail', 1):>4}ms")
                if printed:
                    print("\x1b[4A" + line1, end="", flush=True)
                    print("\n" + line2, end="", flush=True)
                    print("\n" + line3, end="", flush=True)
                    print("\n" + line4, end="", flush=True)
                else:
                    print(line1, end="", flush=True)
                    print("\n" + line2, end="", flush=True)
                    print("\n" + line3, end="", flush=True)
                    print("\n" + line4, end="", flush=True)
                    printed = True
            elif cfg.worker_id == 0:
                # 单行降级 (不依赖 ANSI, conhost 也支持 \r): 每秒 \r 回行首覆盖同一行,
                # 该行包含完整双桶水位 (排队/处理中/完成/失败/速率) + 运行汇总,
                # 实时更新且不刷屏。适合 PyCharm/捕获环境 (与 4 行 ANSI 同样信息量)。
                elapsed = now - started
                hh, mm, ssec = int(elapsed // 3600), int(elapsed % 3600 // 60), int(elapsed % 60)
                total_done = s_done + d_done
                total_fail = s_fail + d_fail
                total_rate = total_done / elapsed if elapsed > 0 else 0
                rk = getattr(risk, "state", "") or "" if risk else ""
                line = (f"\r[运行] {hh:02d}:{mm:02d}:{ssec:02d} "
                        f"[搜索] 排队{ss['queue']:>5} 处理中{ss['processing']:>3} "
                        f"完成{s_done:>6} 失败{s_fail:>2} {s_rate:5.1f}/s | "
                        f"[详情] 排队{ps['queue']:>6} 处理中{ps['processing']:>3} "
                        f"完成{d_done:>6} 失败{d_fail:>2} {d_rate:5.1f}/s | "
                        f"总完成{total_done} 总失败{total_fail} 综合{total_rate:.1f}/s "
                        f"风控:{rk or 'ok'}")
                print(line.ljust(120), end="", flush=True)
                printed = True
    except asyncio.CancelledError:
        if cfg.worker_id == 0 and printed:
            print()  # 换行收尾, 避免残留半行


async def _worker_loop(cfg: WorkerConfig) -> Dict:
    """单个 worker 进程: 搜索消费 + 详情消费 + 崩溃 watchdog + 实时进度。

    搜索/详情分桶限速: 搜索用 search 桶 (IP 信誉敏感), 详情用 detail 桶
    (高频, 实测无风控)。两个 client 各持自己的 rate_limiter。
    """
    search_q = TaskQueue(cfg.redis_url, queue_type=QUEUE_SEARCH)
    pos_q = TaskQueue(cfg.redis_url, queue_type=QUEUE_POSITION)
    limiter_prefix = f"zhaopin:ratelimit:{settings.EGRESS_ID}"
    search_limiter = RedisRateLimiter(
        search_q.redis, cfg.search_rate, key=f"{limiter_prefix}:search")
    detail_limiter = RedisRateLimiter(
        search_q.redis, cfg.detail_rate, key=f"{limiter_prefix}:detail")
    risk = AsyncRedisRiskState(search_q.redis, scope=settings.EGRESS_ID)
    stats = LatencyStats()   # 分环节耗时统计 (实时均值 + 结束总结)
    search_client = AsyncZhilianClient(risk=risk, rate_limiter=search_limiter,
                                       stats=stats, name="search",
                                       max_clients=max(1, cfg.search_concurrency))
    detail_client = AsyncZhilianClient(risk=risk, rate_limiter=detail_limiter,
                                       stats=stats, name="detail",
                                       max_clients=max(1, cfg.concurrency))
    storage = await AsyncStorage.create(cfg.db_url)
    # watchdog 独立 Task: 消费 loop 全退后 cancel, 否则 gather 永不返回 (流程不收敛)
    watchdog = asyncio.create_task(_watchdog(search_q, pos_q))
    monitor = asyncio.create_task(_progress_monitor(search_q, pos_q, cfg,
                                                    risk=risk, stats=stats))
    coros = [
        _consume_search_loop(search_q, search_client, storage, pos_q, cfg, stats=stats)
        for _ in range(max(1, cfg.search_concurrency))
    ]
    coros += [
        _consume_position_loop(pos_q, detail_client, storage, cfg,
                               search_q=search_q, stats=stats)
        for _ in range(cfg.concurrency)
    ]
    try:
        results = await asyncio.gather(*coros)
        # 结束总结 (仅 worker0 打终端, 避免多进程 print 与实时进度 ANSI 覆盖交叠花屏;
        # 非 worker0 只写各自日志文件, 数据不丢)
        stats.print_summary(cfg.worker_id, to_stdout=(cfg.worker_id == 0))
    finally:
        watchdog.cancel()
        monitor.cancel()
        await asyncio.gather(watchdog, monitor, return_exceptions=True)
        await search_client.close()
        await detail_client.close()
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
                             concurrency=args.concurrency,
                             search_concurrency=args.search_concurrency,
                             search_rate=args.search_rate, detail_rate=args.detail_rate,
                             max_attempts=args.max_attempts, worker_id=i)
                for i in range(workers)]
    # 启动摘要走 stdout print (不被 WARNING 过滤, 进度显示器下方一次性显示)
    print(f"启动 {workers} 个 worker 进程 (每进程 {args.concurrency} 并发, "
          f"搜索 {args.search_rate:.0f}/s + 详情 {args.detail_rate:.0f}/s)")
    started = time.time()
    procs = [multiprocessing.Process(target=_worker_main, args=(cfg,)) for cfg in cfg_list]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    failed_workers = [
        f"worker-{i}(exitcode={p.exitcode})"
        for i, p in enumerate(procs) if p.exitcode != 0
    ]
    if failed_workers:
        message = "; ".join(failed_workers)
        logger.error("采集 worker 异常退出: %s", message)
        raise RuntimeError(f"采集未完成: {message}")
    # 任务总耗时 (各 worker 的分环节统计表已各自打印到终端+日志)
    print(f"采集完成: 总耗时 {_fmt_elapsed(time.time() - started)}, 日志见 output/logs/")


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
    print(f"采集日志: {_LOG_FILE}")
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
