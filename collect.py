# -*- coding: utf-8 -*-
"""
智联采集器 — 异步流水线入口 (Phase A: 高并发 + SQLite 入库)

用法:
    py collect.py --kw python,java --jl 530 --pages 5              # 搜索+详情并发入库
    py collect.py --kw python --city 北京 --concurrency 10 --db output/zhaopin.db
    py collect.py --kw python --resume --export output/zhaopin.csv # 中断恢复 + 导出
    py collect.py --export output/zhaopin.csv --db output/zhaopin.db  # 仅导出已入库
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from utils.pipeline import Pipeline, PipelineConfig
from utils.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collect")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="智联采集器 (异步流水线 + SQLite 入库)")
    ap.add_argument("--kw", help="搜索关键词, 逗号分隔")
    ap.add_argument("--jl", help="城市代码 (如 530)")
    ap.add_argument("--city", help="城市中文名 (如 北京)")
    ap.add_argument("--pages", type=int, default=5, help="每关键词页数 (默认 5)")
    ap.add_argument("--concurrency", type=int, default=settings.DETAIL_CONCURRENCY,
                    help="详情并发数 (默认 %d)" % settings.DETAIL_CONCURRENCY)
    ap.add_argument("--search-concurrency", type=int, default=settings.SEARCH_CONCURRENCY,
                    help="搜索并发数 (默认 %d, 风控敏感)" % settings.SEARCH_CONCURRENCY)
    ap.add_argument("--rate", type=float, default=settings.DETAIL_RATE_PER_SEC,
                    help="全局限速 请求/秒 (默认 %.1f)" % settings.DETAIL_RATE_PER_SEC)
    ap.add_argument("--db", default=settings.DB_PATH, help="SQLite 路径 (默认 %s)" % settings.DB_PATH)
    ap.add_argument("--export", help="采集后把 positions 导出为 CSV 路径")
    ap.add_argument("--resume", action="store_true", help="跳过已入库 number (中断恢复)")
    ap.add_argument("--name", default="", help="运行命名 (runs 表标记)")
    return ap.parse_args()


def resolve_city(args) -> str:
    city = args.jl
    if not city:
        city = settings.CITY_CODES.get(args.city or "", settings.DEFAULT_CITY)
    return city


async def _amain(args) -> None:
    # 仅导出模式
    if not args.kw:
        if args.export:
            storage = Storage(args.db)
            n = storage.export_csv(args.export, "positions")
            storage.close()
            logger.info("已导出 %d 行 -> %s", n, args.export)
            return
        logger.error("缺少 --kw, 或用 --export 仅导出")
        sys.exit(1)

    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]
    cfg = PipelineConfig(
        keywords=keywords, city=resolve_city(args), pages=args.pages,
        detail_concurrency=args.concurrency, search_concurrency=args.search_concurrency,
        detail_rate_per_sec=args.rate, db_path=args.db, resume=args.resume, name=args.name,
    )
    logger.info("启动流水线: kw=%s city=%s pages=%d 详情并发=%d 搜索并发=%d 限速=%.1f/s db=%s",
                keywords, cfg.city, cfg.pages, cfg.detail_concurrency,
                cfg.search_concurrency, cfg.detail_rate_per_sec, args.db)
    result = await Pipeline(cfg).run()
    logger.info("结果: %s", result)

    if args.export:
        storage = Storage(args.db)
        n = storage.export_csv(args.export, "positions")
        storage.close()
        logger.info("已导出 %d 行 -> %s", n, args.export)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        logger.warning("收到中断, 流水线已优雅保存进度")


if __name__ == "__main__":
    main()
