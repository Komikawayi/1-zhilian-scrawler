# -*- coding: utf-8 -*-
"""智联招聘搜索采集器 — CSV 输出。"""
from __future__ import annotations

import csv
from typing import Dict, List


def write_csv(path: str, rows: List[Dict[str, str]]) -> None:
    """将职位记录写入 CSV (UTF-8 with BOM, 兼容 Excel)。"""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
