# -*- coding: utf-8 -*-
"""Storage (SQLite) 存储层自检: schema/upsert 去重/统计/导出."""
from __future__ import annotations

import csv

from utils.storage import Storage, POSITION_FIELDS


def _storage(tmp_path):
    return Storage(str(tmp_path / "test.db"))


def _row(num="CCL123", name="职位A", company="公司A"):
    return {"position_number": num, "position_name": name,
            "company_name": company, "company_number": "COM1",
            "salary": "1-2万", "job_desc": "desc",
            "source": "detailv2", "raw_json": '{"a":1}'}


def test_schema_and_count(tmp_path):
    s = _storage(tmp_path)
    assert s.count("positions") == 0
    assert s.count("runs") == 0
    s.close()


def test_upsert_position_dedup(tmp_path):
    s = _storage(tmp_path)
    s.upsert_position(_row())
    s.upsert_position(_row())               # 同 number 再次入库 -> 覆盖更新
    s.upsert_position(_row(num="CCL456"))
    assert s.count("positions") == 2        # 去重: 只有 2 个
    assert "CCL123" in s.get_existing_numbers()
    assert "CCL456" in s.get_existing_numbers()
    s.close()


def test_position_columns_cover_parser_fields():
    """positions 表字段应覆盖 parser 详情输出的核心字段。"""
    from utils.parser import V2_POSITION_MAP
    parser_keys = set(V2_POSITION_MAP.keys())
    # position_number 是主键列 (upsert 单独处理), 其余详情字段应入库
    missing = (parser_keys - set(POSITION_FIELDS)) - {"position_number"}
    assert not missing, f"parser 字段未入库: {missing}"


def test_upsert_search_pool(tmp_path):
    s = _storage(tmp_path)
    s.upsert_search_pool("N1", "python", "530", 1, "职位", "公司", "1-2万")
    s.upsert_search_pool("N1", "java", "530", 1)   # 同 number 覆盖
    assert s.count("search_pool") == 1
    s.close()


def test_upsert_company(tmp_path):
    s = _storage(tmp_path)
    s.upsert_company({"company_number": "COM1", "company_name": "公司A",
                      "company_size": "100-499", "industry_name": "IT"})
    s.upsert_company({"company_number": "COM1", "company_name": "公司A-改名"})
    assert s.count("companies") == 1
    s.close()


def test_runs_start_finish(tmp_path):
    s = _storage(tmp_path)
    rid = s.start_run("pipeline", {"kw": ["python"], "city": "530"})
    assert rid >= 1
    s.finish_run(rid, total=10, success=9, fail=1)
    s.close()
    # 重新打开验证持久化
    s2 = Storage(str(tmp_path / "test.db"))
    import sqlite3
    with s2._lock:
        row = s2.conn.execute("SELECT total,success,fail FROM runs WHERE id=?", (rid,)).fetchone()
    assert row == (10, 9, 1)
    s2.close()


def test_export_csv(tmp_path):
    s = _storage(tmp_path)
    s.upsert_position(_row())
    out = str(tmp_path / "out.csv")
    n = s.export_csv(out, "positions")
    assert n == 1
    with open(out, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["position_number"] == "CCL123"
    assert rows[0]["position_name"] == "职位A"
    s.close()
