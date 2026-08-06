# -*- coding: utf-8 -*-
"""
智联招聘采集器 (纯协议版) — 搜索 SSR + 职位详情

用法:
    # 搜索采集: python 北京(530) 3页
    python main.py --kw python --jl 530 --pages 5
    python main.py --kw python,java --city 北京 --pages 5 --output output/zhaopin.csv

    # 职位详情采集 (推荐: position-detailv2 JSON API, 无挑战)
    python main.py --detail --detail-numbers "CCLxxx,CCLyyy" --output output/zhaopin_detail.csv

    # 职位详情采集 (兜底: SSR + EdgeOne JS Challenge, 需 Node)
    python main.py --detail --detail-urls "https://www.zhaopin.com/jobdetail/xxx.htm" --output out.csv

原理:
    1. sou.zhaopin.com/?jl={城市}&kw={关键词}&p={页码} 会 302 重定向到
       www.zhaopin.com/sou/jl{k城市}/kw{编码关键词}/p{页码} (kw 编码在服务端完成)
    2. 职位数据在 SSR HTML 的 __INITIAL_STATE__.positionList 中
    3. EdgeOne 按 TLS 指纹放行 -> 用 curl_cffi impersonate=chrome 模拟 Chrome 指纹
    4. 详情: position-detailv2 JSON API 纯协议可取 (推荐); SSR 路径首跳是 EdgeOne JS
       Challenge 壳, 本地 Node vm 执行算 EO-Bot-Js-Token 后重放 (兜底)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings  # noqa: E402
from utils.http_client import ZhilianClient  # noqa: E402
from utils.output import write_csv  # noqa: E402
from utils.parser import (  # noqa: E402
    parse_meta, parse_positions, extract_initial_state, parse_job_detail, parse_position_detail_v2,
)
from utils.challenge import fetch_job_detail  # noqa: E402
from utils.fe_api import fetch_position_detail_v2  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="智联招聘采集器 (搜索 SSR + 职位详情, 纯协议)")
    ap.add_argument("--kw", help="搜索关键词, 多个用逗号分隔 (如 python,java)")
    ap.add_argument("--jl", help="城市代码 (如 530), 或 --city 传中文城市名")
    ap.add_argument("--city", help="城市中文名 (如 北京), 与 --jl 二选一")
    ap.add_argument("--pages", type=int, default=1, help="每个关键词采集页数 (默认 1)")
    ap.add_argument("--output", default="output/zhaopin.csv", help="CSV 输出路径")
    ap.add_argument("--sleep", type=float, default=1.5, help="页面间随机等待基础值 (秒)")
    ap.add_argument("--detail", action="store_true", help="职位详情采集模式")
    ap.add_argument("--detail-numbers", help="职位号列表 (逗号分隔), 走 position-detailv2 API (推荐, 无挑战)")
    ap.add_argument("--detail-urls", help="职位详情 URL 列表 (逗号分隔), 走 SSR + EdgeOne 挑战 (兜底)")
    ap.add_argument("--captcha-cooldown", type=float, default=0.0,
                    help="触发 EdgeOne 交互验证码后等待冷却秒数 (默认 0=直接报错)")
    return ap.parse_args()


def collect_detail_v2(client, numbers):
    """position-detailv2 JSON API 采集 (推荐路径, 无需 EdgeOne 挑战)。"""
    rows = []
    for number in numbers:
        number = number.strip()
        if not number:
            continue
        logger.info("抓取职位详情 (v2 API): %s", number)
        data = fetch_position_detail_v2(client, number)
        row = parse_position_detail_v2(data)
        row["position_number"] = number
        rows.append(row)
    return rows


def collect_detail(client, urls, captcha_cooldown):
    """采集职位详情页数据。"""
    rows = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        logger.info("抓取职位详情: %s", url)
        html = fetch_job_detail(client, url, captcha_cooldown=captcha_cooldown)
        state = extract_initial_state(html)
        row = parse_job_detail(state)
        row["position_url"] = url
        rows.append(row)
    return rows


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
    client = ZhilianClient(min_interval=args.sleep * 0.8, max_interval=args.sleep * 1.6)

    # ---- 详情采集模式 ----
    if args.detail:
        if args.detail_numbers:
            # 推荐路径: position-detailv2 JSON API (无挑战)
            numbers = [n for n in args.detail_numbers.split(",") if n.strip()]
            rows = collect_detail_v2(client, numbers)
        elif args.detail_urls:
            # 兜底路径: SSR + EdgeOne JS Challenge
            urls = [u for u in args.detail_urls.split(",") if u.strip()]
            rows = collect_detail(client, urls, args.captcha_cooldown)
        else:
            logger.error("--detail 模式需要 --detail-numbers 或 --detail-urls")
            sys.exit(1)
        if not rows:
            logger.warning("未采集到任何职位详情")
            return
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        write_csv(args.output, rows)
        logger.info("完成: 共 %d 条职位详情 -> %s", len(rows), args.output)
        return

    # ---- 搜索采集模式 ----
    if not args.kw:
        logger.error("缺少 --kw (搜索关键词) 或使用 --detail 进入详情模式")
        sys.exit(1)
    city = args.jl
    if not city:
        if args.city and args.city not in settings.CITY_CODES:
            logger.warning("未知城市名 %r, 回退到默认城市 %s", args.city, settings.DEFAULT_CITY)
        city = settings.CITY_CODES.get(args.city or "", settings.DEFAULT_CITY)
    keywords = [k.strip() for k in args.kw.split(",") if k.strip()]

    # 先做一次连通性/指纹自检
    probe = client.get(f"https://sou.zhaopin.com/?jl={city}&kw={keywords[0]}&p=1")
    if "Security Verification" in probe.text:
        logger.error("EdgeOne 拦截: TLS 指纹模拟未生效，请检查 curl_cffi/网络")
        sys.exit(1)

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
