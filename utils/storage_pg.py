# -*- coding: utf-8 -*-
"""
PostgreSQL 存储层 (asyncpg) — 百万级数据入库 (生产主存储)

相较 SQLite(Phase A, 已移除):
  - 原生并发写: 多 worker 进程并发 upsert, 无单写者瓶颈
  - 连接池: asyncpg pool (min_size/max_size)
  - JSONB: raw_json 结构化存储
  - 索引: company_number / city_id / fetched_at (百万级查询)

方法签名对齐 utils/storage.py (存储抽象契约), 但全部 async。
用法:
  storage = await AsyncStorage.create(url)
  await storage.upsert_position(row)
  await storage.close()
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from typing import Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)

# positions 字段 (对齐 utils/storage.py POSITION_FIELDS)
POSITION_FIELDS = [
    "position_name", "position_url", "salary", "salary_real", "salary_type",
    "work_type", "working_exp", "education", "recruit_number", "city_id",
    "work_city", "city_district", "publish_time", "job_type", "job_desc",
    "work_address", "latitude", "longitude", "company_name", "company_number",
    "company_root_id", "position_highlight", "rpo_proxy", "can_regular",
    "can_remote_internship", "welfare_tags", "labels", "skill_tags", "task_id",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  position_number TEXT PRIMARY KEY,
  {pos_cols},
  source TEXT,
  raw_json JSONB,
  fetched_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_positions_company ON positions(company_number);
CREATE INDEX IF NOT EXISTS idx_positions_city ON positions(city_id);
CREATE INDEX IF NOT EXISTS idx_positions_fetched ON positions(fetched_at);

CREATE TABLE IF NOT EXISTS search_pool (
  number TEXT PRIMARY KEY,
  keyword TEXT, city TEXT, page INT,
  position_name TEXT, company_name TEXT, salary_display TEXT,
  fetched_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_search_keyword ON search_pool(keyword, city);

CREATE TABLE IF NOT EXISTS companies (
  company_number TEXT PRIMARY KEY,
  company_name TEXT, company_size TEXT, financing_stage TEXT,
  industry_name TEXT, description TEXT,
  fetched_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
  id BIGSERIAL PRIMARY KEY,
  mode TEXT, params_json JSONB, started_at TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ, total INT, success INT, fail INT
);
"""


def _to_raw(row: dict):
    """raw_json 处理: 统一转为 JSON 字符串 (asyncpg jsonb codec 期望 str, 非裸 dict)。

    注意: 必须先截断再序列化, 不能 json.dumps 后 [:N] 截断 — 会切断字符串中间
    产生非法 JSON (如长 jobDesc 内含引号), 导致 asyncpg InvalidTextRepresentationError。
    """
    raw = row.get("raw_json")
    if raw is None:
        return None
    if isinstance(raw, (dict, list, tuple)):
        return json.dumps(raw, ensure_ascii=False)
    return str(raw)   # 已是 JSON 字符串


class AsyncStorage:
    """PostgreSQL 存储 (asyncpg 连接池)。"""

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.url = ""

    @classmethod
    async def create(cls, url: str, pool_max: int = 20) -> "AsyncStorage":
        self = cls()
        self.url = url
        self.pool = await asyncpg.create_pool(url, min_size=2, max_size=pool_max)
        await self._init_schema()
        return self

    async def _init_schema(self) -> None:
        cols = ",\n  ".join(f"{c} TEXT" for c in POSITION_FIELDS)
        async with self.pool.acquire() as conn:
            await conn.execute(_SCHEMA.format(pos_cols=cols))

    # ---- 写入 ----

    async def upsert_position(self, row: Dict[str, str]) -> None:
        num = row.get("position_number") or row.get("task_id")
        if not num:
            logger.warning("upsert_position 缺 number, 跳过: %s", str(row)[:80])
            return
        fields = [f for f in POSITION_FIELDS if f in row and f != "position_number"]
        cols = ["position_number"] + fields + ["source", "raw_json"]
        vals = [str(num)] + [str(row.get(f, "") or "") for f in fields]
        vals += [str(row.get("source", "") or ""), _to_raw(row)]
        ph = ",".join(f"${i}" for i in range(1, len(cols) + 1))
        updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols[1:])
        sql = f"INSERT INTO positions ({','.join(cols)}) VALUES ({ph}) " \
              f"ON CONFLICT(position_number) DO UPDATE SET {updates}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *vals)

    async def upsert_search_pool(self, number, keyword, city, page,
                                 title="", company="", salary="") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO search_pool (number, keyword, city, page, position_name, company_name, salary_display) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT(number) DO UPDATE SET fetched_at=now()",
                number, keyword, city, page, title, company, salary)

    async def upsert_company(self, row: Dict[str, str]) -> None:
        num = row.get("company_number")
        if not num:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO companies (company_number, company_name, company_size, financing_stage, industry_name, description) "
                "VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT(company_number) DO UPDATE SET "
                "company_name=EXCLUDED.company_name, company_size=EXCLUDED.company_size, "
                "financing_stage=EXCLUDED.financing_stage, industry_name=EXCLUDED.industry_name, "
                "description=EXCLUDED.description, fetched_at=now()",
                num, str(row.get("company_name", "") or ""), str(row.get("company_size", "") or ""),
                str(row.get("financing_stage", "") or ""), str(row.get("industry_name", "") or ""),
                str(row.get("company_description", "") or ""))

    async def upsert_company_mini(self, company_number: str, company_name: str) -> None:
        """只写公司号+名 (搜索 worker 先落库, 供 company: 任务消费时查名)。

        公司详情由详情 worker 的 upsert_company 补全; 此处 ON CONFLICT 不覆盖已有名。
        """
        if not company_number or not company_name:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO companies (company_number, company_name) VALUES ($1,$2) "
                "ON CONFLICT(company_number) DO UPDATE SET "
                "company_name=EXCLUDED.company_name, fetched_at=now()",
                company_number, company_name)

    async def get_company_name(self, company_number: str) -> str:
        """按公司号查公司名 (company: 任务消费用); 不存在返回空串。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT company_name FROM companies WHERE company_number=$1", company_number)
            return row["company_name"] if row else ""

    # ---- 查询 ----

    async def get_existing_numbers(self) -> set:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT position_number FROM positions")
            return {r["position_number"] for r in rows}

    async def count(self, table: str = "positions") -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(f"SELECT COUNT(*) FROM {table}")

    # ---- 运行统计 ----

    async def start_run(self, mode: str, params: dict) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO runs (mode, params_json) VALUES ($1,$2) RETURNING id",
                mode, json.dumps(params, ensure_ascii=False))

    async def finish_run(self, run_id: int, total: int, success: int, fail: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE runs SET finished_at=now(), total=$2, success=$3, fail=$4 WHERE id=$1",
                run_id, total, success, fail)

    # ---- 导出 ----

    async def export_csv(self, path: str, table: str = "positions") -> int:
        """把表导出为 CSV (UTF-8 BOM)。返回行数。"""
        async with self.pool.acquire() as conn:
            cols = [c["name"] for c in await conn.fetch(f"SELECT column_name AS name FROM information_schema.columns WHERE table_name=$1", table)]
            rows = await conn.fetch(f"SELECT * FROM {table}")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([str(r[c]) if r[c] is not None else "" for c in cols])
        logger.info("导出 %s -> %s (%d 行)", table, path, len(rows))
        return len(rows)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
