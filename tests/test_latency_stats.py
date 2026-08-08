# -*- coding: utf-8 -*-
"""LatencyStats 分环节耗时统计收集器 — 单元测试 (纯同步, 不依赖网络)。"""
from __future__ import annotations

import unicodedata

from utils.latency_stats import LatencyStats, _cjk_pad


def _disp_width(text: str) -> int:
    """CJK-aware 显示宽度 (中文字符按 2 半角宽), 与 _cjk_pad 同口径。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _row(rows, stage: str) -> dict:
    return next(r for r in rows if r["stage"] == stage)


def test_record_summary_basic():
    """record 后 summary 的 count/mean/p50/p95/max 正确。"""
    s = LatencyStats()
    s.record("detail_parse", 0.001)   # 1ms
    s.record("detail_parse", 0.002)   # 2ms
    r = _row(s.summary(), "detail_parse")
    assert r["count"] == 2
    assert r["mean"] == 1.5
    # sorted [1,2]: p50 idx=int(2*0.5)=1 -> 2.0; p95 idx=1 -> 2.0
    assert r["p50"] == 2.0
    assert r["p95"] == 2.0
    assert r["max"] == 2.0


def test_record_summary_single_sample():
    """样本=1 退化: p50=p95=max=该值。"""
    s = LatencyStats()
    s.record("cooldown", 3.0)         # 3000ms
    r = _row(s.summary(), "cooldown")
    assert r["count"] == 1
    assert r["mean"] == r["p50"] == r["p95"] == r["max"] == 3000.0


def test_record_net_total_ttfb():
    """record_net 存 {stage}_net 总耗时 + TTFB, current_mean 可读。"""
    s = LatencyStats()
    s.record_net("search", 0.45, 0.40)   # total 450ms, ttfb 400ms
    s.record_net("search", 0.50, 0.44)   # total 500ms, ttfb 440ms
    assert s.current_mean("search_net") == 475.0
    assert s.current_mean("search_ttfb") == 420.0
    # summary: search_net 行附 ttfb_mean
    r = _row(s.summary(), "search_net")
    assert r["count"] == 2
    assert r["mean"] == 475.0
    assert r["ttfb_mean"] == 420.0


def test_record_net_none_ignored():
    """record_net total 为 None (无 CURLINFO) 时忽略, 不崩。"""
    s = LatencyStats()
    s.record_net("search", None, None)
    assert s.current_mean("search_net") is None
    assert s.summary() == []


def test_empty_summary():
    """无样本时 format_table 不崩, 显示无样本。"""
    s = LatencyStats()
    text = s.format_table(0)
    assert "=== worker 0 分环节耗时统计 ===" in text
    assert "无样本" in text
    s.print_summary(0)   # 不抛异常


def test_format_table_header_aligned():
    """表头含环节/样本/均值/p50/p95/max/TTFB, 数据行与表头视觉宽度一致。"""
    s = LatencyStats()
    s.record_net("search", 0.45, 0.40)
    s.record("detail_parse", 0.001)
    lines = s.format_table(1).splitlines()
    header, data_row = lines[1], lines[2]
    assert "环节" in header and "样本" in header and "p50" in header
    assert "搜索网络" in data_row
    assert "450ms" in data_row and "(TTFB)" in header
    # 表头与数据行显示宽度一致 (CJK-aware 列宽固定, 视觉严格对齐)
    assert _disp_width(header) == _disp_width(data_row)


def test_cjk_pad():
    """CJK-aware 补全: 中文字符按 2 半角宽计。"""
    assert _cjk_pad("搜索网络", 10) == "搜索网络  "      # 8 宽 + 2 空格
    assert _cjk_pad("search", 10) == "search    "       # 6 宽 + 4 空格
    assert _cjk_pad("详情入库", 10) == "详情入库  "
