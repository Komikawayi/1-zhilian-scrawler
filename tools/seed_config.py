# -*- coding: utf-8 -*-
"""
配置播种 — 生成 config/keywords.json (关键词) + config/cities.json (城市)

数据来源:
  keywords: 51job-crawler config/51job_city_profiles.json (112 制造词, 去城市名前缀)
  cities:   js_reverse_cache/data/search_base_data.json data.allCity (智联全城市)

用法:
  py tools/seed_config.py [--keyword-src <path>] [--city-src <path>]
  py tools/seed_config.py --dry-run    # 只打印统计, 不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

# 51job 城市 profile 路径 (桌面项目, 跨盘引用)
DEFAULT_KEYWORD_SRC = r"external-city-profile.json"
DEFAULT_CITY_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "js_reverse_cache", "data", "search_base_data.json")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")

# 需要播种的 tier (跳过 L2_region_enhanced: "区域+词" 组合, 剥离后与 L1 重复)
_SEED_TIERS = ("L1_core", "L3_process_extended", "L4_manufacturing_context")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="配置播种 (keywords/cities)")
    ap.add_argument("--keyword-src", default=DEFAULT_KEYWORD_SRC, help="51job profiles 路径")
    ap.add_argument("--city-src", default=DEFAULT_CITY_SRC, help="智联 search_base_data 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计不写文件")
    return ap.parse_args()


def extract_keywords(path: str) -> List[str]:
    """从 51job city profiles 提取去重关键词 (L1/L3/L4, 去城市名前缀)。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cities = d.get("cities", {})
    kws = set()
    for cv in cities.values():
        for tier, words in (cv.get("query_tiers") or {}).items():
            if tier not in _SEED_TIERS:
                continue
            for w in words:
                bare = w.strip()
                city_name = cv.get("city_name", "")
                if bare.startswith(city_name):
                    bare = bare[len(city_name):]
                bare = bare.strip()
                if bare:
                    kws.add(bare)
    return sorted(kws)


# 直辖市: 在 allCity 中作为省级存在 (sublist 是区县), 需本身也收录为城市
_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")


def extract_cities(path: str) -> List[dict]:
    """从智联 search_base_data 提取全量城市 (省→市→区县 递归, 只要市级及以上)。

    直辖市 (北京/上海/天津/重庆) 在 allCity 是省级节点, 本身收录;
    普通省取 sublist 市级。
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    provinces = d.get("data", {}).get("allCity") or []
    out = []
    for prov in provinces:
        if prov.get("deleted"):
            continue
        name = prov.get("name") or ""
        if name in _MUNICIPALITIES:
            out.append({"name": name, "code": prov.get("code")})
            continue
        sub = prov.get("sublist") or []
        for city in sub:
            if city.get("deleted"):
                continue
            out.append({"name": city.get("name"), "code": city.get("code")})
    return out


def main() -> None:
    args = parse_args()
    kws = extract_keywords(args.keyword_src)
    cities = extract_cities(args.city_src)
    print(f"关键词: {len(kws)} 个")
    print(f"城市: {len(cities)} 个")
    if args.dry_run:
        print("关键词样本:", ", ".join(kws[:15]), "...")
        print("城市样本:", ", ".join("{}({})".format(c["name"], c["code"]) for c in cities[:10]), "...")
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    kw_path = os.path.join(OUT_DIR, "keywords.json")
    city_path = os.path.join(OUT_DIR, "cities.json")
    with open(kw_path, "w", encoding="utf-8") as f:
        json.dump({"keywords": kws, "source": "51job_city_profiles"}, f,
                  ensure_ascii=False, indent=2)
    with open(city_path, "w", encoding="utf-8") as f:
        json.dump({"cities": cities, "source": "search_base_data.allCity"}, f,
                  ensure_ascii=False, indent=2)
    print(f"已写入 {kw_path}")
    print(f"已写入 {city_path}")


if __name__ == "__main__":
    main()
