# -*- coding: utf-8 -*-
"""
公司聚合分析 — 从 PostgreSQL 按公司聚合全部在招岗位

用途: 数据分析筛选。语义: "该公司已采集到的在招岗位" (搜索命中 + 公司名补采的并集)。

用法:
  py tools/company_aggregate.py --top 20                 # 岗位数 TOP20 公司
  py tools/company_aggregate.py --company CZ883210900    # 某公司全部岗位
  py tools/company_aggregate.py --city 653               # 某城市全部岗位
  py tools/company_aggregate.py --kw smt                 # 岗位名含 smt
  py tools/company_aggregate.py --export out.csv         # 导出 CSV
  py tools/company_aggregate.py --company-file list.txt  # 公司号清单 (每行一个)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from utils.storage_pg import AsyncStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("aggregate")

# 聚合输出字段 (positions 常用列, 按需扩展)
OUT_FIELDS = [
    "position_number", "position_name", "company_number", "company_name",
    "salary", "salary_real", "work_city", "city_id", "working_exp",
    "education", "publish_time", "job_desc",
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="公司岗位聚合 (查 PG)")
    ap.add_argument("--company", help="公司号 (单个)")
    ap.add_argument("--company-file", help="公司号清单文件 (每行一个)")
    ap.add_argument("--city", help="城市代码筛选 (如 653)")
    ap.add_argument("--kw", help="岗位名关键词筛选 (LIKE)")
    ap.add_argument("--top", type=int, default=0, help="岗位数 TOP N 公司 (0=全部)")
    ap.add_argument("--export", help="导出 CSV 路径")
    ap.add_argument("--db", default=settings.DB_URL, help="PostgreSQL URL")
    return ap.parse_args()


def _where(args: argparse.Namespace) -> tuple[str, List]:
    """构造 WHERE 条件 + 参数 (SQL 注入安全: 全参数化)。"""
    conds, params = [], []
    if args.company:
        conds.append("company_number=$%d" % (len(params) + 1))
        params.append(args.company)
    if args.city:
        conds.append("city_id=$%d" % (len(params) + 1))
        params.append(args.city)
    if args.kw:
        conds.append("position_name ILIKE $%d" % (len(params) + 1))
        params.append(f"%{args.kw}%")
    if args.company_file:
        with open(args.company_file, encoding="utf-8") as f:
            nums = [l.strip() for l in f if l.strip()]
        if nums:
            conds.append("company_number = ANY($%d)" % (len(params) + 1))
            params.append(nums)
    where = " WHERE " + " AND ".join(conds) if conds else ""
    return where, params


def _print_rows(rows: List[Dict], fields: List[str]) -> None:
    """终端表格输出 (截断长字段)。"""
    widths = {f: max(len(f), max((len(str(r[f])) for r in rows), default=0)) for f in fields}
    header = "  ".join(f.ljust(widths[f])[:24] for f in fields)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[f])[:24].ljust(widths[f])[:24] for f in fields))


async def _amain(args) -> None:
    storage = await AsyncStorage.create(args.db)
    try:
        where, params = _where(args)
        # 公司聚合统计
        if args.top:
            rows = await storage.pool.fetch(
                f"""SELECT company_number, company_name, COUNT(*) AS job_cnt
                    FROM positions{where}
                    GROUP BY company_number, company_name
                    ORDER BY job_cnt DESC
                    LIMIT $%d""" % (len(params) + 1),
                *params, args.top)
            print(f"=== 岗位数 TOP {args.top} 公司 ===")
            for r in rows:
                print(f"  {r['company_name'][:24]:26s} {r['company_number']:18s} {r['job_cnt']} 岗位")
            return

        # 岗位明细
        rows = await storage.pool.fetch(
            f"SELECT {','.join(OUT_FIELDS)} FROM positions{where} ORDER BY company_number, position_number",
            *params)
        print(f"=== 命中 {len(rows)} 条岗位 ===")
        if rows:
            _print_rows(rows, OUT_FIELDS[:8])
        if args.export:
            os.makedirs(os.path.dirname(args.export) or ".", exist_ok=True)
            with open(args.export, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(OUT_FIELDS)
                for r in rows:
                    w.writerow([str(r[c]) if r[c] is not None else "" for c in OUT_FIELDS])
            logger.info("已导出 %d 行 -> %s", len(rows), args.export)
    finally:
        await storage.close()


def main() -> None:
    args = parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
