# -*- coding: utf-8 -*-
"""
SQLite 存储层 — 爬取结果入库 (去重 / 增量 / 运行统计 / 导出)

单机 Phase A 用 SQLite (WAL, 零依赖)。存储抽象接口预留 PostgreSQL:
  Storage 的方法签名即契约, 未来换后端只替换本模块实现。

写路径: 单连接 + threading.Lock (WAL 并发读); asyncio 流水线用
  asyncio.to_thread 调用, 避免阻塞事件循环。

表:
  positions    详情主表 (position_number PK, 跨 run 去重/增量)
  search_pool  搜索池 (number 来源, 跨关键词去重)
  companies    公司表 (company_number PK)
  runs         采集运行统计
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# positions 表字段 (对齐 utils/parser.py 详情输出 + 元数据)
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
  raw_json TEXT,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS search_pool (
  number TEXT PRIMARY KEY,
  keyword TEXT, city TEXT, page INTEGER,
  position_name TEXT, company_name TEXT, salary_display TEXT,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  company_number TEXT PRIMARY KEY,
  company_name TEXT, company_size TEXT, financing_stage TEXT,
  industry_name TEXT, description TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT, params_json TEXT, started_at TEXT, finished_at TEXT,
  total INTEGER, success INTEGER, fail INTEGER
);
"""


class Storage:
    """SQLite 存储: 去重入库 / 运行统计 / 查询导出。线程安全 (WAL + lock)。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        cols = ",\n  ".join(f"{c} TEXT" for c in POSITION_FIELDS)
        with self._lock:
            self.conn.executescript(_SCHEMA.format(pos_cols=cols))
            self.conn.commit()

    # ---- 写入 ----

    def upsert_position(self, row: Dict[str, str]) -> None:
        """详情 upsert (position_number 唯一, 跨 run 去重)。row 含 position_number + 详情字段。"""
        num = row.get("position_number") or row.get("task_id")
        if not num:
            logger.warning("upsert_position 缺 number, 跳过: %s", str(row)[:80])
            return
        fields = [f for f in POSITION_FIELDS if f in row and f != "position_number"]
        cols = ["position_number"] + fields + ["source", "raw_json", "fetched_at"]
        vals = [str(num)] + [str(row.get(f, "") or "") for f in fields]
        vals += [str(row.get("source", "") or ""), str(row.get("raw_json", "") or ""),
                 time.strftime("%Y-%m-%d %H:%M:%S")]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO positions ({','.join(cols)}) VALUES ({placeholders}) " \
              f"ON CONFLICT(position_number) DO UPDATE SET " \
              f"{','.join(f'{c}=excluded.{c}' for c in cols[1:])}"
        with self._lock:
            self.conn.execute(sql, vals)
            self.conn.commit()

    def upsert_search_pool(self, number: str, keyword: str, city: str, page: int,
                           title: str = "", company: str = "", salary: str = "") -> None:
        """搜索池记录 (number 唯一)。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO search_pool (number, keyword, city, page, position_name, company_name, salary_display, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(number) DO UPDATE SET fetched_at=excluded.fetched_at",
                (number, keyword, city, page, title, company, salary,
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.conn.commit()

    def upsert_company(self, row: Dict[str, str]) -> None:
        """公司 upsert (company_number 唯一)。"""
        num = row.get("company_number")
        if not num:
            return
        cols = ["company_number", "company_name", "company_size",
                "financing_stage", "industry_name", "description", "fetched_at"]
        vals = [str(num), str(row.get("company_name", "") or ""), str(row.get("company_size", "") or ""),
                str(row.get("financing_stage", "") or ""), str(row.get("industry_name", "") or ""),
                str(row.get("company_description", "") or ""), time.strftime("%Y-%m-%d %H:%M:%S")]
        placeholders = ",".join("?" * len(cols))
        with self._lock:
            self.conn.execute(
                f"INSERT INTO companies ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(company_number) DO UPDATE SET "
                f"{','.join(f'{c}=excluded.{c}' for c in cols[1:])}", vals)
            self.conn.commit()

    # ---- 查询 ----

    def get_existing_numbers(self) -> set:
        """已入库的 position_number 集合 (resume 去重用)。"""
        with self._lock:
            cur = self.conn.execute("SELECT position_number FROM positions")
            return {r[0] for r in cur.fetchall()}

    def count(self, table: str = "positions") -> int:
        with self._lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- 运行统计 ----

    def start_run(self, mode: str, params: dict) -> int:
        """记录一次采集运行, 返回 run_id。"""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs (mode, params_json, started_at) VALUES (?,?,?)",
                (mode, json.dumps(params, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, total: int, success: int, fail: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, total=?, success=?, fail=? WHERE id=?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), total, success, fail, run_id))
            self.conn.commit()

    # ---- 导出 ----

    def export_csv(self, path: str, table: str = "positions") -> int:
        """把表导出为 CSV (UTF-8 BOM)。返回行数。"""
        with self._lock:
            cur = self.conn.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
        logger.info("导出 %s -> %s (%d 行)", table, path, len(rows))
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
