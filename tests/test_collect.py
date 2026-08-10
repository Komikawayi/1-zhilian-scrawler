# -*- coding: utf-8 -*-
"""collect.py 辅助函数自检 (不依赖网络/DB)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from collect import (
    RAW_JSON_MAX, WorkerConfig, _consume, _consume_position_loop, _split_csv,
    _trim_raw_json,
)
from utils.async_client import AsyncZhilianClient, PositionUnavailableError


def test_trim_raw_json_valid():
    """超长字段截断后仍是合法 JSON。"""
    data = {
        "a": "x" * 9000,          # 超长字符串
        "nested": {"jobDesc": "y" * 9000 + '"quote"', "n": 1},
        "list": ["z" * 9000, 2],
        "num": 3.14,
    }
    raw = _trim_raw_json(data)
    json.loads(raw)      # 必须可解析
    assert len(raw) <= RAW_JSON_MAX * 3 + 200  # 截断生效 (非整段 3w+)


def test_trim_raw_json_short_untouched():
    """短数据原样序列化, 不截断。"""
    data = {"a": "short", "b": [1, 2], "c": {"d": "e"}}
    raw = _trim_raw_json(data)
    assert json.loads(raw) == data


def test_split_csv_accepts_fullwidth_comma():
    assert _split_csv("smt，pcba, 贴片") == ["smt", "pcba", "贴片"]


def test_async_client_exposes_connection_pool_size():
    """详情并发不能被 curl_cffi 默认 max_clients=10 静默截断。"""
    with patch("utils.async_client.cffi_requests.AsyncSession") as session:
        client = AsyncZhilianClient(max_clients=37)
        assert client.max_clients == 37
        assert session.call_args.kwargs["max_clients"] == 37
        client.session.close = AsyncMock()


def test_worker_process_failure_propagates():
    """子进程非零退出时主进程不能打印成功并返回 0。"""
    class FailedProcess:
        exitcode = 7

        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self):
            pass

    args = SimpleNamespace(
        workers=1, redis="redis://test", db="postgresql://test",
        concurrency=10, search_concurrency=10, search_rate=20,
        detail_rate=80, max_attempts=3,
    )
    with patch("collect.multiprocessing.Process", FailedProcess):
        with pytest.raises(RuntimeError, match="exitcode=7"):
            _consume(args)


def test_position_211_uses_bounded_queue_retry():
    """高并发下偶发 211 不能直接永久丢弃岗位。"""
    async def _():
        queue = AsyncMock()
        queue.dequeue.side_effect = ["position:ABC", None]
        queue.is_drained.return_value = True
        cfg = WorkerConfig(redis_url="redis://test", db_url="postgresql://test",
                           max_attempts=3)
        with patch("collect.fetch_position_detail_v2_async",
                   side_effect=PositionUnavailableError("211")):
            result = await _consume_position_loop(
                queue, AsyncMock(), AsyncMock(), cfg)
        queue.fail.assert_awaited_once_with("position:ABC", max_attempts=3)
        queue.fail_permanent.assert_not_awaited()
        assert result == {"ok": 0, "fail": 1}

    asyncio.run(_())
