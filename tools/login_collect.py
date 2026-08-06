# -*- coding: utf-8 -*-
"""
智联招聘登录态采集工具 — 批量采集登录态接口数据

登录态经 URL 参数 at/rt 传递 (config/zhilian-session.local.json, gitignored)。

用法:
    py tools/login_collect.py [--session config/zhilian-session.local.json]
                              [--output output/login_state.json]
                              [--sleep 1.5]

采集: 简历列表 / 未读消息 / 投递·面试统计 / VIP / 导航
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.http_client import ZhilianClient
from utils.session import load_session
from utils.fe_api import (
    fetch_resume_list, fetch_unread_message, fetch_resume_feedback_count,
    fetch_vip_info, fetch_navigation_list, fetch_resume_diagnosis, LoginExpiredError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("login_collect")


def collect(client, session):
    """采集登录态数据, 返回 dict。会话过期时抛 LoginExpiredError。"""
    logger.info("会话: %s", session.source)
    data = {"account": session.source}

    rl = fetch_resume_list(client, session)
    data["resume_list"] = rl if isinstance(rl, list) else []
    logger.info("简历列表: %d 份", len(data["resume_list"]) if isinstance(rl, list) else 0)

    data["unread_message"] = fetch_unread_message(client, session)
    data["feedback_count"] = fetch_resume_feedback_count(client, session)
    data["vip_info"] = fetch_vip_info(client, session)
    data["navigation"] = fetch_navigation_list(client, session)

    # 简历诊断 (用第一份完整简历)
    if isinstance(rl, list) and rl:
        first = rl[0]
        rid, rnum = first.get("id"), first.get("number")
        if rid and rnum:
            try:
                data["resume_diagnosis"] = fetch_resume_diagnosis(client, session, rnum, str(rid))
            except Exception as e:
                logger.warning("简历诊断失败: %s", e)
                data["resume_diagnosis"] = None

    logger.info("消息=%s 投递/面试=%s", data["unread_message"], data["feedback_count"])
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="智联登录态采集")
    ap.add_argument("--session", default=None, help="会话文件 (默认 config/zhilian-session.local.json)")
    ap.add_argument("--output", default="output/login_state.json", help="输出 JSON 路径")
    ap.add_argument("--sleep", type=float, default=1.5, help="请求间隔")
    args = ap.parse_args()

    session = load_session(args.session)
    if not session:
        logger.error("会话加载失败, 请先捕获登录会话 (见 js_reverse_cache/tasks/zhilian-risk-phase0/report.md)")
        sys.exit(1)

    client = ZhilianClient(min_interval=args.sleep * 0.8, max_interval=args.sleep * 1.6)
    try:
        data = collect(client, session)
    except LoginExpiredError as e:
        logger.error("会话过期: %s", e)
        logger.error("请重新捕获 at/rt (CloakBrowser 登录后更新 config/zhilian-session.local.json)")
        sys.exit(2)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存 -> %s", args.output)


if __name__ == "__main__":
    main()
