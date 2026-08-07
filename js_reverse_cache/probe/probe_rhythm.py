# -*- coding: utf-8 -*-
"""
Phase 2c: 请求节奏对照实验 — 防线升级阈值 + 登录态对照

保守参数保护 IP 信誉 (实验污染纪律: IP 被标记后所有对照作废)。

测量:
  A. 基线: 搜索/详情/fe-api(匿名 vs 登录态) 当前防线状态
  B. 详情页频率梯度: 低频/中频/高频 各 N 次, 观察 JS Challenge 是否升级验证码
  C. 登录态 fe-api 节奏: 带 at/rt 高频, 与匿名对比

用法:
  py js_reverse_cache/probe_rhythm.py [--light]
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curl_cffi import requests as cr
from utils.session import load_session
from utils.fe_api import fetch_unread_message

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

DETAIL = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm"
SEARCH = "https://sou.zhaopin.com/?jl=530&kw=python&p=1"


def classify(t):
    if "__INITIAL_STATE__" in t:
        return "DATA"
    if "Security Verification" in t:
        return "CAPTCHA"
    if "solveChallenge" in t:
        return "CHALLENGE"
    return "OTHER"


def probe(html):
    """探测一个 HTML 的防线状态。"""
    return classify(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", action="store_true", help="轻量模式 (少请求)")
    args = ap.parse_args()

    print("=== A. 基线探测 ===")
    # 搜索页
    r = cr.get(SEARCH, impersonate="chrome", timeout=25, allow_redirects=True)
    print(f"  搜索页: {probe(r.text)} (len={len(r.text)})")
    # 详情页
    r = cr.get(DETAIL, impersonate="chrome", timeout=25)
    print(f"  详情页: {probe(r.text)} (len={len(r.text)})")
    # fe-api 匿名
    r = cr.get("https://fe-api.zhaopin.com/c/i/search/base/data?_v=0.1&x-zp-page-request-id=t&x-zp-client-id=t",
               headers={"x-zp-business-system": "1", "x-zp-page-code": "4019", "x-zp-platform": "13"},
               impersonate="chrome", timeout=25)
    print(f"  fe-api匿名: {'OK' if r.status_code == 200 else r.status_code}")
    # fe-api 登录态
    session = load_session()
    if session:
        try:
            from utils.http_client import ZhilianClient
            client = ZhilianClient()
            v = fetch_unread_message(client, session)
            print(f"  fe-api登录态: OK unread={v}")
        except Exception as e:
            print(f"  fe-api登录态: ERR {type(e).__name__} {str(e)[:60]}")
    else:
        print("  fe-api登录态: 无会话")

    print("\n=== B. 详情页频率梯度 ===")
    # 低频 (2s), 中频 (1s), 高频 (0.3s) 各 5 次 (light 模式各 3 次)
    for label, interval, n in [("低频2s", 2.0, 5), ("中频1s", 1.0, 5), ("高频0.3s", 0.3, 5)]:
        if args.light and interval < 1.0:
            n = 3
        results = []
        for i in range(n):
            r = cr.get(DETAIL, impersonate="chrome", timeout=25)
            results.append(probe(r.text))
            time.sleep(interval)
        # 升级检测: 首次出现 CAPTCHA
        upgraded = "CAPTCHA" in results
        print(f"  {label}: {results} {'⚠️升级验证码' if upgraded else '✅稳定'}")
        if upgraded:
            print("  → 已触发验证码, 停止更高频测试 (IP 信誉保护)")
            break

    print("\n=== C. 登录态 fe-api 高频对照 ===")
    if session:
        from utils.http_client import ZhilianClient
        client = ZhilianClient()
        n = 8 if not args.light else 4
        results = []
        try:
            for i in range(n):
                v = fetch_unread_message(client, session)
                results.append("OK" if v is not None else "ERR")
                time.sleep(0.3)
            print(f"  登录态 unread-message x{n} @0.3s: {results} {'✅稳定' if all(x=='OK' for x in results) else '⚠️异常'}")
        except Exception as e:
            print(f"  登录态高频: ERR {type(e).__name__} {str(e)[:60]}")
    else:
        print("  无会话, 跳过")

    print("\n=== 结论 ===")
    print("  防线升级点: 参考上方 B 段首次 CAPTCHA 出现位置")


if __name__ == "__main__":
    main()
