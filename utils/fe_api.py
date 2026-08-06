# -*- coding: utf-8 -*-
"""
智联招聘 fe-api JSON 接口 — 纯协议请求 (无需 EdgeOne 挑战/签名)

动态参数 (_v / x-zp-page-request-id / x-zp-client-id) 服务端不校验, 随机生成即可。

推荐路径: position-detailv2 职位详情 JSON API, 实测 20/20 稳定,
无挑战事件、无验证码、无 IP 信誉依赖 (见 docs/zhilian-edgeone-reverse-analysis.md §6.5)。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Dict

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

# fe-api 固定请求头
FE_API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-zp-business-system": "1",
    "x-zp-page-code": "4019",
    "x-zp-platform": "13",
    "referer": "https://www.zhaopin.com/",
}

DETAIL_V2_URL = "https://fe-api.zhaopin.com/c/i/jobs/position-detailv2"


def _fe_api_params(**extra) -> dict:
    """构造 fe-api 动态参数 (非签名, 随机即可)。"""
    params = {
        "_v": "%.8f" % (time.time() % 1),
        "x-zp-page-request-id": uuid.uuid4().hex + "-" + str(int(time.time() * 1000)),
        "x-zp-client-id": str(uuid.uuid4()),
        "platform": "13",
        "version": "0.0.0",
    }
    params.update(extra)
    return params


def _check_ok(body: Dict, name: str) -> None:
    """校验 fe-api 通用响应码, 非 200 抛 ConnectionError。"""
    if body.get("code") != 200 or body.get("apiCode") != 200:
        raise ConnectionError(
            f"{name} 业务错误 code={body.get('code')} apiCode={body.get('apiCode')} msg={body.get('message')}"
        )


def fetch_position_detail_v2(client, number: str) -> Dict:
    """
    职位详情 JSON API (推荐路径, 无需 EdgeOne 挑战/Node/验证码)。

    Args:
        client: ZhilianClient 实例
        number: 职位号 (搜索页 positionList.number, 如 CCL1480117890J40614881205)

    Returns:
        position-detailv2 响应 data 部分 (原始 dict, 解析交给上层 parse_position_detail_v2)
    """
    resp = client.get(DETAIL_V2_URL, headers=FE_API_HEADERS, params=_fe_api_params(number=number))
    if resp.status_code != 200:
        raise ConnectionError(f"position-detailv2 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception as e:
        raise ConnectionError(f"position-detailv2 响应非 JSON: {e}") from e
    _check_ok(body, "position-detailv2")
    return body.get("data") or {}
