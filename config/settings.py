# -*- coding: utf-8 -*-
"""智联招聘搜索采集器 — 默认配置。"""
from __future__ import annotations

import json as _json
import os as _os

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
DETAIL_CONCURRENCY = 10          # 每 worker 详情并发协程数；连接池显式跟随该值
SEARCH_CONCURRENCY = 15          # 每 worker 搜索并发协程数
DETAIL_RATE_PER_SEC = 80.0       # 详情桶保守生产值；提升前需持续压测
SEARCH_RATE_PER_SEC = 20.0       # 搜索桶限速；当前短测约 25/s 后延迟明显升高
QUEUE_SIZE = 200                 # 队列容量 (背压)
# ---- PostgreSQL 存储 (百万级, 隔离部署: zhilian-net, 127.0.0.1:5433) ----
# URL 优先级: 环境变量 ZHAOPIN_DB_URL > config/db.local.json (gitignored) > 默认
def _load_db_url() -> str:
    env = _os.environ.get("ZHAOPIN_DB_URL")
    if env:
        return env
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "db.local.json")
    if _os.path.exists(p):
        try:
            return _json.load(open(p, encoding="utf-8")).get("db_url", "")
        except Exception:
            pass
    return "postgresql://zhilian:zhilian@127.0.0.1:5433/zhilian"


DB_URL = _load_db_url()          # PostgreSQL 连接串 (asyncpg)
DB_POOL_MAX = 20                 # 连接池上限 (多 worker 并发写)

# ---- Redis 分布式任务队列 (Phase B: 万级, 隔离部署) ----
REDIS_URL = "redis://127.0.0.1:6379/0"   # 隔离的 zhilian-redis 容器 (zhilian-net, 仅本机)
EGRESS_ID = _os.environ.get("ZHAOPIN_EGRESS_ID", "default")
WORKERS = 2                               # 默认消费 worker 进程数
MAX_ATTEMPTS = 3                          # 任务失败重试次数 (Redis 队列)
TASK_POOL_CAP = 20000                     # 单任务池上限 (万级护栏)
