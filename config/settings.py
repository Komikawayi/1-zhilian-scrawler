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

# ---- 采集运行配置 ----
# 风控纪律: 智联按 IP 信誉分级, 并发/速率是共享风险资源。
# 主路径 (搜索 SSR + position-detailv2) 低频串行即可稳定; 高并发易升级防线。
CONCURRENCY = 1          # 并发数 (1=串行; 不建议>3)
MIN_INTERVAL = 1.2       # 单请求最小间隔 (秒)
MAX_INTERVAL = 2.5       # 单请求最大间隔 (秒)
RETRIES = 3              # 请求重试次数 (指数退避)
TIMEOUT = 25             # 请求超时 (秒)

# ---- 异步流水线 (Phase A: 高并发 + SQLite 入库, 见 collect.py) ----
# 详情 position-detailv2 无 IP 信誉依赖 (448/448 压测零升级), 可高并发;
# 搜索 SSR 风控敏感, 保持低频。
DETAIL_CONCURRENCY = 10          # 详情并发 worker 数
SEARCH_CONCURRENCY = 2           # 搜索并发 worker 数 (风控敏感)
DETAIL_RATE_PER_SEC = 8.0        # 全局限速 (请求/秒, 令牌桶跨 worker 共享)
QUEUE_SIZE = 200                 # 队列容量 (背压)
DB_PATH = "output/zhaopin.db"    # SQLite 入库路径 (WAL 模式)

# ---- Redis 分布式任务队列 (Phase B: 万级, 隔离部署) ----
REDIS_URL = "redis://127.0.0.1:6379/0"   # 隔离的 zhilian-redis 容器 (zhilian-net, 仅本机)
WORKERS = 2                               # 默认消费 worker 进程数
MAX_ATTEMPTS = 3                          # 任务失败重试次数 (Redis 队列)
TASK_POOL_CAP = 20000                     # 单任务池上限 (万级护栏)
