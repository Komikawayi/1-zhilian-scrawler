# -*- coding: utf-8 -*-
"""智联招聘搜索采集器 — 默认配置。"""
from __future__ import annotations

# 常见城市代码 (zhaopin 城市 code)
CITY_CODES = {
    "北京": "530",
    "上海": "538",
    "广州": "763",
    "深圳": "765",
    "杭州": "653",
    "南京": "635",
    "武汉": "736",
    "成都": "801",
    "西安": "854",
    "全国": "0",
}

DEFAULT_CITY = "530"
DEFAULT_PAGE_SIZE = 20  # 每页职位数 (服务端 SSR 固定 20)
