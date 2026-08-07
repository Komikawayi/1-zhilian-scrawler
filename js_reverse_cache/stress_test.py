# -*- coding: utf-8 -*-
"""
单 IP 规模化压测 — 匿名 + 路线一 (搜索 SSR → position-detailv2)

目标: 用数据回答"单 IP 到底能不能稳定爬"
  - 500 个唯一 number (5 关键词 × 5 页 SSR)
  - 500 条 position-detailv2 匿名连续采集 (1.2-2.5s/条)
  - 监测: 成功率 / 风控状态转移 / 总耗时 / 速率因子 / 防线升级

用法: python js_reverse_cache/stress_test.py
输出: output/stress_search.csv / output/stress_detail.csv + 控制台 checkpoint
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from utils.http_client import ZhilianClient
from utils.risk import RiskState, STATE_CHALLENGE, STATE_COOLING, STATE_OK
from utils.parser import extract_initial_state, parse_positions, parse_position_detail_v2
from utils.fe_api import fetch_position_detail_v2
from utils.output import write_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("stress")

KEYWORDS = ["python", "java", "golang", "前端开发", "测试工程师"]
CITY = "530"          # 北京
PAGES = 5             # 每关键词页数 -> 5*20=100/keyword, 5关键词=500
OUT_DIR = Path(__file__).resolve().parent.parent / "output"


def phase1_search(client, risk):
    """搜索池: 5 关键词 × 5 页 SSR, 收集唯一 number。"""
    logger.info("== Phase 1: 搜索池 ==")
    numbers, rows = [], []
    for kw in KEYWORDS:
        for page in range(1, PAGES + 1):
            html = client.fetch_search_page(kw, CITY, page)
            state = extract_initial_state(html)
            for r in parse_positions(state):
                r["keyword"] = kw
                r["city_code"] = CITY
                rows.append(r)
                n = r.get("position_number")
                if n:
                    numbers.append(n)
            logger.info("  %s p%d: +%d (累计 number %d)", kw, page, len(parse_positions(state)), len(set(numbers)))
            time.sleep(0.3)
    unique = list(dict.fromkeys(numbers))
    logger.info("Phase 1 完成: 搜索行 %d, 唯一 number %d", len(rows), len(unique))
    write_csv(str(OUT_DIR / "stress_search.csv"), rows)
    return unique


def phase2_detail(client, risk, numbers):
    """500 条 position-detailv2 匿名采集。"""
    logger.info("== Phase 2: 详情采集 (%d 条) ==", len(numbers))
    ok, fail, rows = 0, 0, []
    t0 = time.time()
    states = {}
    for i, num in enumerate(numbers, 1):
        try:
            data = fetch_position_detail_v2(client, num)
            row = parse_position_detail_v2(data)
            row["position_number"] = num
            rows.append(row)
            ok += 1
        except Exception as e:
            fail += 1
            logger.warning("  [%d/%d] %s 失败: %s", i, len(numbers), num, e)
        # checkpoint + 状态监测
        states[risk.state] = states.get(risk.state, 0) + 1
        if i % 100 == 0:
            el = time.time() - t0
            rate = i / el if el else 0
            logger.info("  CHECKPOINT %d/%d: 成功%d 失败%d 耗时%.0fs (%.2f条/s) 风控状态=%s",
                        i, len(numbers), ok, fail, el, rate, risk.state)
            logger.info("    状态分布=%s 挑战连续=%d 冷却计数=%d 速率因子=%.1f",
                        states, risk.challenge_streak, risk.captcha_count, risk.rate_factor())
    el = time.time() - t0
    logger.info("== Phase 2 完成: 成功%d 失败%d 总耗时%.0fs (%.2f条/s) 最终状态=%s ==",
                ok, fail, el, ok / el if el else 0, risk.state)
    write_csv(str(OUT_DIR / "stress_detail.csv"), rows)
    return {"ok": ok, "fail": fail, "elapsed": el, "states": states,
            "challenge_streak": risk.challenge_streak, "captcha_count": risk.captcha_count}


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    risk = RiskState()
    client = ZhilianClient(min_interval=1.2, max_interval=2.5, risk=risk)
    t_all = time.time()
    numbers = phase1_search(client, risk)
    detail = phase2_detail(client, risk, numbers)
    risk.save()
    total_el = time.time() - t_all
    logger.info("== 总耗时 %.0fs ==", total_el)
    logger.info("== 结果: 搜索 %d 唯一 number, 详情 成功%d/失败%d ==",
                len(numbers), detail["ok"], detail["fail"])
    logger.info("== 风控观测: 挑战连续=%d 冷却计数=%d 状态分布=%s ==",
                detail["challenge_streak"], detail["captcha_count"], detail["states"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
