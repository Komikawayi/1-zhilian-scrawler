# -*- coding: utf-8 -*-
"""
智联采集器 — 一键运行配置脚本 (交互式向导 + 自动运行)

流程:
  1. 交互式逐项填写 ([默认值], 回车用默认):
     · 关键词: 逗号分隔, 或从 keywords.json 选择 (种子 28 制造词, 可自定义)
     · 城市:   中文名或代码, 逗号分隔, 或从 cities.json 选择 (456 城市)
     · 页数:   每关键词搜索页数
     · 模式:   1=Redis 分布式 (默认, 推荐)  2=单机流水线
     · [Redis] worker数 / 并发 / 限速 / 是否清池
     · 运行命名
  2. 打印配置摘要 + 确认 (Y/n)
  3. 确认 → 自动运行 (城市×关键词 笛卡尔积 → produce → consume)

机器区分: 上次配置记录 hostname, 本机与上次不一致时告警 (多台开发机区分配置)。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

# Windows 控制台编码修正: 强制 UTF-8 (中文城市名/关键词在 GBK 终端不乱码)
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import settings

# 配置持久化 (gitignored via config/*.local.json; 记录本机 hostname 区分多机)
RUN_CONF = ROOT / "config" / "run.local.json"
KEYWORDS_CONF = ROOT / "config" / "keywords.json"
CITIES_CONF = ROOT / "config" / "cities.json"

DEFAULT_KEYWORDS = ["smt", "pcba", "贴片", "回流焊"]
DEFAULT_CITY = "530"   # 北京


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
        if label.startswith("关键词") or label.startswith("城市"):
            # 允许回车跳过 (后续从列表选)
            return ""
        print("  ⚠ 必填项, 不能为空")


def _select_from_list(title: str, items: list, multi: bool = True) -> str:
    """从列表选择 (多选/单选), 回车返回。"""
    print(f"\n{title} (共 {len(items)} 项):")
    for i, it in enumerate(items[:30]):
        print(f"  {i+1:3d}. {it}")
    if len(items) > 30:
        print(f"  ... 共 {len(items)} 项, 仅显示前 30")
    while True:
        inp = input("输入序号(逗号分隔多个, 0=全部, 回车=跳过): ").strip()
        if not inp:
            return ""
        if inp == "0":
            return ",".join(items)
        try:
            idxs = [int(x) - 1 for x in inp.split(",")]
            if all(0 <= i < len(items) for i in idxs):
                return ",".join(items[i] for i in idxs)
        except ValueError:
            pass
        print("  ⚠ 序号无效, 重新输入")


def _select_keywords() -> str:
    """关键词: 手动输入 或 从种子列表选。"""
    kw = _prompt("关键词", "", "逗号分隔, 如 smt,pcba,贴片")
    if kw:
        return kw
    seed = _load_json(KEYWORDS_CONF, {}).get("keywords") or DEFAULT_KEYWORDS
    print("从种子列表选择 (51job 制造词, 可编辑 config/keywords.json 补充):")
    return _select_from_list("关键词", seed)


def _select_cities() -> str:
    """城市: 手动输入 或 从全城市列表选。"""
    city = _prompt("城市", "", "中文名或代码, 逗号分隔, 如 北京,上海")
    if city:
        return city
    cities = _load_json(CITIES_CONF, {}).get("cities") or []
    names = [c["name"] for c in cities]
    print("从智联城市列表选择 (456 城市):")
    return _select_from_list("城市", names)


def _confirm(conf: dict) -> bool:
    """打印配置摘要 + 确认。"""
    print("\n" + "=" * 50)
    print("配置摘要:")
    for k, v in conf.items():
        if isinstance(v, list):
            print(f"  {k}: {', '.join(str(x) for x in v[:10])}{'...' if len(v)>10 else ''}")
        else:
            print(f"  {k}: {v}")
    print("=" * 50)
    ans = input("确认运行? (Y/n): ").strip().lower()
    return ans in ("", "y", "yes")


def _run_redis(conf: dict) -> int:
    """Redis 分布式: produce (城市×关键词×页数) → consume (多进程)。"""
    kw = ",".join(conf["keywords"])
    cities = ",".join(conf["cities"])
    # produce
    cmd = [sys.executable, str(ROOT / "collect.py"), "--produce",
           "--kw", kw, "--cities", cities,
           "--pages", str(conf["pages"])]
    if conf.get("clear"):
        cmd.append("--clear")
    print(f"\n>>> produce: {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("❌ produce 失败, 中止"); return r.returncode
    # consume
    cmd = [sys.executable, str(ROOT / "collect.py"), "--consume",
           "--workers", str(conf["workers"]),
           "--concurrency", str(conf["concurrency"]),
           "--rate", str(conf["rate"]),
           "--name", conf.get("name", "")]
    print(f">>> consume: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _run_single(conf: dict) -> int:
    """单机流水线 (--kw --cities 直接跑 pipeline)。"""
    kw = ",".join(conf["keywords"])
    cities = ",".join(conf["cities"])
    cmd = [sys.executable, str(ROOT / "collect.py"),
           "--kw", kw, "--cities", cities,
           "--pages", str(conf["pages"]),
           "--concurrency", str(conf["concurrency"]),
           "--rate", str(conf["rate"]),
           "--name", conf.get("name", "")]
    print(f">>> {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def main() -> int:
    # 机器区分告警
    prev = _load_run_conf()
    if prev.get("hostname") and prev["hostname"] != socket.gethostname():
        print(f"⚠ 上次配置来自 {prev['hostname']}, 本机是 {socket.gethostname()}, 配置可能不适用!")
        if input("继续? (Y/n): ").strip().lower() not in ("", "y", "yes"):
            return 0

    print("=" * 50)
    print("智联采集器一键运行向导")
    print("=" * 50)

    # 1. 关键词
    kw_str = _select_keywords()
    if not kw_str:
        print("未选择关键词, 退出"); return 0
    keywords = [k.strip() for k in kw_str.split(",") if k.strip()]

    # 2. 城市
    city_str = _select_cities()
    if not city_str:
        print("未选择城市, 退出"); return 0
    # 中文名转代码 (cities.json), 代码直接透传
    cities_map = {c["name"]: c["code"] for c in _load_json(CITIES_CONF, {}).get("cities", [])}
    cities = []
    for c in city_str.split(","):
        c = c.strip()
        if not c:
            continue
        cities.append(cities_map.get(c, c))   # 名转码, 码原样

    # 3. 页数
    pages = _prompt("每关键词页数", str(settings.DEFAULT_PAGES if hasattr(settings, "DEFAULT_PAGES") else 5))
    try:
        pages = max(1, int(pages))
    except ValueError:
        pages = 5

    # 4. 模式
    mode = _prompt("模式", "1", "1=Redis分布式 2=单机流水线").strip()
    is_redis = mode != "2"          # 默认 Redis; 2=单机

    # 5. 运行参数
    workers = int(_prompt("worker 进程数", str(settings.WORKERS)))
    concurrency = int(_prompt("每进程并发", str(settings.DETAIL_CONCURRENCY)))
    rate = float(_prompt("全局限速 req/s", str(settings.DETAIL_RATE_PER_SEC)))
    clear_in = _prompt("produce 前清空队列", "n", "y/n").lower()
    clear = clear_in in ("y", "yes")
    name = _prompt("运行命名", "", "可选, 标记 runs 表")

    conf = {
        "keywords": keywords, "cities": cities, "pages": pages,
        "mode": "redis" if is_redis else "single",
        "workers": workers, "concurrency": concurrency,
        "rate": rate, "clear": clear, "name": name,
    }
    if not _confirm(conf):
        print("已取消"); return 0
    _save_run_conf(conf)
    return _run_redis(conf) if is_redis else _run_single(conf)


if __name__ == "__main__":
    sys.exit(main())
