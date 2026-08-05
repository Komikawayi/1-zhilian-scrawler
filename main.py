# -*- coding: utf-8 -*-
"""
智联招聘搜索采集器 (纯协议版)

用法:
    python main.py --kw python --jl 530 --pages 5
    python main.py --kw python,java --city 北京 --pages 5 --output output/zhaopin.csv

原理:
    1. sou.zhaopin.com/?jl={城市}&kw={关键词}&p={页码} 会 302 重定向到
       www.zhaopin.com/sou/jl{k城市}/kw{编码关键词}/p{页码} (kw 编码在服务端完成)
    2. 职位数据在 SSR HTML 的 __INITIAL_STATE__.positionList 中
    3. EdgeOne 按 TLS 指纹放行 -> 用 curl_cffi impersonate=chrome 模拟 Chrome 指纹
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from curl_cffi import requests as cffi_requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings  # noqa: E402
from utils.http_client import ZhilianClient  # noqa: E402
from utils.output import write_csv  # noqa: E402
from utils.parser import parse_meta, parse_positions, extract_initial_state  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="智联招聘搜索采集器 (纯协议)")
    ap.add_argument("--kw", required=True, help="搜索关键词, 多个用逗号分隔 (如 python,java)")
    ap.add_argument("--jl", help="城市代码 (如 530), 或 --city 传中文城市名")
    ap.add_argument("--city", help="城市中文名 (如 北京), 与 --jl 二选一")
    ap.add_argument("--pages", type=int, default=1, help="每个关键词采集页数 (默认 1)")
    ap.add_argument("--output", default="output/zhaopin.csv", help="CSV 输出路径")
    ap.add_argument("--sleep", type=float, default=1.5, help="页面间随机等待基础值 (秒)")
    return ap.parse_args()


def collect(
    client: ZhilianClient,
    keyword: str,
    city: str,
    pages: int,
) -> list[dict]:
    """采集单个关键词 N 页职位数据。"""
    rows: list[dict] = []
    first_page_state = None
    for page in range(1, pages + 1):
        html = client.fetch_search_page(keyword, city, page)
        state = extract_initial_state(html)
        if first_page_state is None:
            first_page_state = state
        page_rows = parse_positions(state)
        logger.info("kw=%s 城市=%s 第%d/%d页 抓取 %d 条 (总 %s)",
                    keyword, city, page, pages, len(page_rows),
                    state.get("positionCount"))
        rows.extend(page_rows)
    if rows:
        # 补充关键词/城市信息
        meta = parse_meta(first_page_state) if first_page_state else {}
        for r in rows:
            r["keyword"] = keyword
            r["city_code"] = city
    return rows


def main() -> None:
    args = parse_args()
    city = args.jl or settings.CITY_CODES.get(args.city or "", settings.DEFAULT_CITY)
    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]

    # 先做一次连通性/指纹自检
    probe = cffi_requests.get(
        f"https://sou.zhaopin.com/?jl={city}&kw={keywords[0]}&p=1",
        impersonate="chrome", timeout=25, allow_redirects=True,
    )
    if "Security Verification" in probe.text:
        logger.error("EdgeOne 拦截: TLS 指纹模拟未生效，请检查 curl_cffi/网络")
        sys.exit(1)

    client = ZhilianClient(min_interval=args.sleep * 0.8, max_interval=args.sleep * 1.6)
    all_rows: list[dict] = []
    for kw in keywords:
        logger.info("开始采集关键词: %s 城市: %s 页数: %d", kw, city, args.pages)
        all_rows.extend(collect(client, kw, city, args.pages))
        time.sleep(0.5)

    if not all_rows:
        logger.warning("未采集到任何职位数据")
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    write_csv(args.output, all_rows)
    logger.info("完成: 共 %d 条职位 -> %s", len(all_rows), args.output)


if __name__ == "__main__":
    main()
