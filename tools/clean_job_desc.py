# -*- coding: utf-8 -*-
"""
一次性脚本: 清洗存量 positions.job_desc 的 HTML 标签为纯文本。

背景: 早期入库的 job_desc 保留了智联详情接口的 HTML (<br>/<div>/<p>...),
新数据已由 utils/parser.py::clean_job_desc 在入库时清洗为纯文本。
本脚本把存量行补齐 (原始 HTML 在 raw_json 字段仍可回溯)。

用法:
  py tools/clean_job_desc.py --dry-run    # 只预览待清洗行, 不修改
  py tools/clean_job_desc.py              # 全量清洗 (分批 UPDATE, 幂等可重跑)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from utils.parser import clean_job_desc

BATCH = 500          # 每批 UPDATE 条数
LIKE_HTML = "%<%"    # 待清洗判定: 含任何 `<` (标签)


async def main(dry_run: bool) -> None:
    conn = await asyncpg.connect(settings.DB_URL)
    try:
        rows = await conn.fetch(
            "SELECT position_number, job_desc FROM positions WHERE job_desc LIKE $1",
            LIKE_HTML)
        total = len(rows)
        print(f"待清洗: {total} 行 (job_desc 含 HTML 标签)")
        if total == 0:
            print("无需清洗")
            return
        if dry_run:
            print("[dry-run] 不执行 UPDATE, 预览前 3 条:")
            for num, jd in rows[:3]:
                print(f"\n--- {num} ---")
                print(f"  原: {jd[:160]!r}")
                print(f"  清: {clean_job_desc(jd)[:160]!r}")
            return

        updates = []
        unchanged = 0
        for num, jd in rows:
            cleaned = clean_job_desc(jd)
            if cleaned == jd:
                unchanged += 1      # 清洗后无变化 (无实际标签或本就是文本)
                continue
            updates.append((num, cleaned))

        t0 = time.time()
        for i in range(0, len(updates), BATCH):
            batch = updates[i:i + BATCH]
            await conn.executemany(
                "UPDATE positions SET job_desc=$2 WHERE position_number=$1", batch)
            print(f"  已清洗 {min(i + BATCH, len(updates))}/{len(updates)}...")
        elapsed = time.time() - t0
        print(f"完成: 清洗 {len(updates)} 行, 无变化 {unchanged} 行, 耗时 {elapsed:.1f}s")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="清洗存量 positions.job_desc 的 HTML")
    ap.add_argument("--dry-run", action="store_true", help="只预览待清洗行, 不修改")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
