# -*- coding: utf-8 -*-
"""collect.py 辅助函数自检 (不依赖网络/DB)."""
from __future__ import annotations

import json

from collect import RAW_JSON_MAX, _trim_raw_json


def test_trim_raw_json_valid():
    """超长字段截断后仍是合法 JSON。"""
    data = {
        "a": "x" * 9000,          # 超长字符串
        "nested": {"jobDesc": "y" * 9000 + '"quote"', "n": 1},
        "list": ["z" * 9000, 2],
        "num": 3.14,
    }
    raw = _trim_raw_json(data)
    parsed = json.loads(raw)      # 必须可解析
    assert len(raw) <= RAW_JSON_MAX * 3 + 200  # 截断生效 (非整段 3w+)


def test_trim_raw_json_short_untouched():
    """短数据原样序列化, 不截断。"""
    data = {"a": "short", "b": [1, 2], "c": {"d": "e"}}
    raw = _trim_raw_json(data)
    assert json.loads(raw) == data
