# -*- coding: utf-8 -*-
"""
智联采集器 — 一键运行配置脚本 (默认全量自动运行, 可手动定制)

流程:
  1. 启动即显示配置摘要 (默认 = 全城市 × 全关键词, 回车直接跑)
  2. 回车确认 → 自动运行 Redis 分布式 (produce → consume)
  3. 想定制时: 输入 n 进入手动向导 (选关键词/城市/页数/参数)

机器区分: 上次配置记录 hostname, 本机与上次不一致时告警 (多台开发机区分配置)。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

from config import settings

# Windows 控制台编码修正: 强制 UTF-8 (中文城市名/关键词在 GBK 终端不乱码)
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent

# 配置持久化 (gitignored via config/*.local.json; 记录本机 hostname 区分多机)
RUN_CONF = ROOT / "config" / "run.local.json"
KEYWORDS_CONF = ROOT / "config" / "keywords.json"
CITIES_CONF = ROOT / "config" / "cities.json"

# 默认运行参数 (与 settings 对齐)
DEFAULT_PAGES = 5
DEFAULT_WORKERS = settings.WORKERS
DEFAULT_CONCURRENCY = settings.DETAIL_CONCURRENCY
DEFAULT_SEARCH_CONCURRENCY = settings.SEARCH_CONCURRENCY
DEFAULT_RATE = settings.DETAIL_RATE_PER_SEC
DEFAULT_SEARCH_RATE = settings.SEARCH_RATE_PER_SEC


def _load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_run_conf(conf: dict) -> None:
    conf["hostname"] = socket.gethostname()
    with open(RUN_CONF, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)


def _load_run_conf() -> dict:
    return _load_json(RUN_CONF, {})


def _load_keywords() -> list:
    """关键词: keywords.json 全量 (无则用默认制造词)。"""
    kws = _load_json(KEYWORDS_CONF, {}).get("keywords") or []
    return kws if kws else ["smt", "pcba", "贴片", "回流焊"]


def _load_cities() -> list:
    """城市: cities.json 全量 (name+code 列表)。"""
    return _load_json(CITIES_CONF, {}).get("cities") or []


def _prompt(label: str, default: str = "", hint: str = "") -> str:
    """交互提示: 显示 [默认值], 回车用默认。"""
    d = f"[{default}]" if default else ""
    h = f" ({hint})" if hint else ""
    while True:
        val = input(f"{label}{h}{d}: ").strip()
        if val:
            return val
        if default:
            return default
        print("  ⚠ 必填项, 不能为空")


def _confirm(conf: dict) -> bool:
    """打印配置摘要 + 确认。"""
    print("\n" + "=" * 60)
    print("运行配置摘要:")
    for k, v in conf.items():
        if isinstance(v, list):
            shown = v[:10]
            more = f" ...共 {len(v)} 项" if len(v) > 10 else ""
            print(f"  {k}: {', '.join(str(x) for x in shown)}{more}")
        else:
            print(f"  {k}: {v}")
    print("=" * 60)
    ans = input("确认运行? (回车=运行 / n=手动定制): ").strip().lower()
    return ans in ("", "y", "yes")


def _run_redis(conf: dict) -> int:
    """Redis 分布式: produce (城市×关键词) → consume (多进程)。"""
    kw = ",".join(conf["keywords"])
    cities = ",".join(conf["cities"])
    # produce (keyword 任务自动翻完所有页, 无页数上限)
    cmd = [sys.executable, str(ROOT / "collect.py"), "--produce",
           "--kw", kw, "--cities", cities]
    if conf.get("clear"):
        cmd.append("--clear")
    print(f"\n>>> produce: {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("❌ produce 失败, 中止")
        return r.returncode
    # consume (分桶限速: 搜索/详情独立)
    cmd = [sys.executable, str(ROOT / "collect.py"), "--consume",
           "--workers", str(conf["workers"]),
           "--concurrency", str(conf["concurrency"]),
           "--search-concurrency", str(conf["search_concurrency"]),
           "--search-rate", str(conf["search_rate"]),
           "--detail-rate", str(conf["detail_rate"]),
           "--name", conf.get("name", "")]
    print(f">>> consume: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _run_single(conf: dict) -> int:
    """单机流水线 (调试用)。"""
    kw = ",".join(conf["keywords"])
    cities = ",".join(conf["cities"])
    cmd = [sys.executable, str(ROOT / "collect.py"), "--single",
           "--kw", kw, "--cities", cities,
           "--pages", str(conf["pages"]),
           "--concurrency", str(conf["concurrency"]),
           "--rate", str(conf["rate"]),
           "--name", conf.get("name", "")]
    print(f">>> {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _default_conf() -> dict:
    """默认配置: 优先复用上次保存的 run.local.json (同机), 否则全城市 × 全关键词。"""
    prev = _load_run_conf()
    if prev.get("hostname") == socket.gethostname() and prev.get("keywords") and prev.get("cities"):
        # 复用上次配置 (hostname 同机才复用, 跨机告警后走全量)
        return {
            "keywords": prev["keywords"],
            "cities": prev["cities"],
            "city_names": prev.get("city_names", []),
            "pages": prev.get("pages", DEFAULT_PAGES),
            "mode": prev.get("mode", "redis"),
            "workers": prev.get("workers", DEFAULT_WORKERS),
            "concurrency": prev.get("concurrency", DEFAULT_CONCURRENCY),
            "search_concurrency": prev.get("search_concurrency", DEFAULT_SEARCH_CONCURRENCY),
            "rate": prev.get("rate", DEFAULT_RATE),
            "detail_rate": prev.get("detail_rate", DEFAULT_RATE),
            "search_rate": prev.get("search_rate", DEFAULT_SEARCH_RATE),
            "clear": prev.get("clear", False),
            "name": prev.get("name", ""),
        }
    cities = _load_cities()
    keywords = _load_keywords()
    return {
        "keywords": keywords,
        "cities": [c["code"] for c in cities],
        "city_names": [c["name"] for c in cities],
        "pages": DEFAULT_PAGES,
        "mode": "redis",
        "workers": DEFAULT_WORKERS,
        "concurrency": DEFAULT_CONCURRENCY,
        "search_concurrency": DEFAULT_SEARCH_CONCURRENCY,
        "rate": DEFAULT_RATE,
        "detail_rate": DEFAULT_RATE,
        "search_rate": DEFAULT_SEARCH_RATE,
        "clear": False,
        "name": "",
    }


def _manual_wizard(prev: dict) -> dict:
    """手动定制向导 (输入 n 后进入): 选关键词/城市/参数。"""
    print("\n--- 手动定制模式 ---")
    # 关键词: 逗号分隔 或 0=全部
    kws = _load_keywords()
    kw_in = _prompt("关键词", "0", "逗号分隔 或 0=全部")
    keywords = kws if kw_in == "0" else [k.strip() for k in kw_in.split(",") if k.strip()]

    # 城市: 中文名/代码 逗号分隔 或 0=全部
    cities_all = _load_cities()
    cities_map = {c["name"]: c["code"] for c in cities_all}
    city_in = _prompt("城市", "0", "中文名或代码, 逗号分隔 或 0=全部")
    if city_in == "0":
        cities = [c["code"] for c in cities_all]
        city_names = [c["name"] for c in cities_all]
    else:
        cities, city_names = [], []
        for c in city_in.split(","):
            c = c.strip()
            if not c:
                continue
            cities.append(cities_map.get(c, c))
            city_names.append(c)

    # 页数/参数
    pages = int(_prompt("每关键词页数", str(DEFAULT_PAGES)))
    mode = _prompt("模式", "1", "1=Redis分布式 2=单机流水线").strip()
    workers = int(_prompt("worker 进程数", str(DEFAULT_WORKERS)))
    concurrency = int(_prompt("详情并发/进程", str(DEFAULT_CONCURRENCY)))
    search_concurrency = int(_prompt("搜索并发/进程", str(DEFAULT_SEARCH_CONCURRENCY)))
    rate = float(_prompt("单机模式全局限速 req/s", str(DEFAULT_RATE)))
    detail_rate = float(_prompt("Redis 详情桶 req/s", str(DEFAULT_RATE)))
    search_rate = float(_prompt("Redis 搜索桶 req/s", str(DEFAULT_SEARCH_RATE)))
    clear_in = _prompt("produce 前清空队列", "n", "y/n").lower()
    name = _prompt("运行命名", "", "可选, 标记 runs 表")

    return {
        "keywords": keywords,
        "cities": cities,
        "city_names": city_names,
        "pages": pages,
        "mode": "single" if mode == "2" else "redis",
        "workers": workers,
        "concurrency": concurrency,
        "search_concurrency": search_concurrency,
        "rate": rate,
        "detail_rate": detail_rate,
        "search_rate": search_rate,
        "clear": clear_in in ("y", "yes"),
        "name": name,
    }


def main() -> int:
    # 机器区分告警
    prev = _load_run_conf()
    if prev.get("hostname") and prev["hostname"] != socket.gethostname():
        print(f"⚠ 上次配置来自 {prev['hostname']}, 本机是 {socket.gethostname()}, 配置可能不适用!")
        if input("继续? (Y/n): ").strip().lower() not in ("", "y", "yes"):
            return 0

    print("=" * 60)
    print("智联采集器一键运行")
    print("=" * 60)

    conf = _default_conf()
    if not conf["keywords"]:
        print("❌ config/keywords.json 无关键词, 请先运行 py tools/seed_config.py")
        return 1
    if not conf["cities"]:
        print("❌ config/cities.json 无城市, 请先运行 py tools/seed_config.py")
        return 1

    if not _confirm(conf):
        conf = _manual_wizard(prev)

    _save_run_conf(conf)
    return _run_redis(conf) if conf["mode"] == "redis" else _run_single(conf)


if __name__ == "__main__":
    sys.exit(main())
