# -*- coding: utf-8 -*-
"""智联招聘搜索采集器 — CSV 输出。"""
from __future__ import annotations

import csv
from typing import Dict, List


def write_csv(path: str, rows: List[Dict[str, str]]) -> None:
    """将职位记录写入 CSV (UTF-8 with BOM, 兼容 Excel)。"""
    if not rows:
        return
    # 字段名取所有行的并集 (保序去重), 行缺字段自动填空, 避免单行结构不一致报错
    fieldnames = list(dict.fromkeys(k for r in rows for k in r.keys()))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
