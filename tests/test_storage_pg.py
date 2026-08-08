# -*- coding: utf-8 -*-
"""PostgreSQL 存储层自检 (独立测试库 zhilian_test, 不污染主库 zhilian)."""
from __future__ import annotations

import asyncio

import asyncpg
import pytest

from config import settings
from utils.storage_pg import AsyncStorage

TEST_DB = "zhilian_test"


@pytest.fixture(scope="session", autouse=True)
def pg_available():
    """PG 不可用时跳过全部存储测试 (CI/无容器环境不崩)。"""
    async def _ping():
        try:
            conn = await asyncpg.connect(_base_url())
        except Exception:
            pytest.skip("PostgreSQL 不可用 (需启动隔离的 zhilian-postgres 容器)",
                        allow_module_level=True)
        else:
            await conn.close()
    asyncio.run(_ping())


def _run(coro):
    return asyncio.run(coro)


def _base_url(db=TEST_DB) -> str:
    """把 settings.DB_URL 的库名替换为测试库。"""
    url = settings.DB_URL
    # postgresql://user:pw@host:port/db  -> 换库名
    head, _, _rest = url.rpartition("/")
    return f"{head}/{db}"


def _ensure_test_db():
    """确保 zhilian_test 库存在。"""
    async def _():
        maint = settings.DB_URL.rsplit("/", 1)[0] + "/postgres"
        conn = await asyncpg.connect(maint)
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", TEST_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        await conn.close()
    _run(_())


def _clear_tables(url):
    """清空测试库表 (跨运行隔离, 避免残留数据干扰断言)。"""
    async def _():
        s = await AsyncStorage.create(url)
        try:
            for t in ("positions", "companies", "runs"):
                await s.pool.execute(f'TRUNCATE TABLE {t}')
        finally:
            await s.close()
    _run(_())


def test_upsert_position_dedup():
    _ensure_test_db()
    url = _base_url()
    _clear_tables(url)

    async def _():
        s = await AsyncStorage.create(url)
        try:
            await s.upsert_position({"position_number": "TEST1", "position_name": "职位A",
                                     "raw_json": {"a": 1}})
            await s.upsert_position({"position_number": "TEST1", "position_name": "职位A改",
                                     "company_number": "COM1"})
            nums = await s.get_existing_numbers()
            assert "TEST1" in nums
            assert await s.count("positions") == 1   # 去重
            assert await s.count("companies") == 0
            # company 单独入库
            await s.upsert_company({"company_number": "COM1", "company_name": "公司1"})
            assert await s.count("companies") == 1
        finally:
            await s.close()
    _run(_())


def test_runs_and_export(tmp_path):
    _ensure_test_db()
    url = _base_url()
    _clear_tables(url)

    async def _():
        s = await AsyncStorage.create(url)
        try:
            rid = await s.start_run("pipeline", {"kw": ["python"]})
            assert rid >= 1
            await s.finish_run(rid, total=10, success=9, fail=1)
            out = str(tmp_path / "out.csv")
            n = await s.export_csv(out, "positions")
            assert n >= 0
        finally:
            await s.close()
    _run(_())
