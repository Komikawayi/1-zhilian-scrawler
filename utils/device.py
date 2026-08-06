# -*- coding: utf-8 -*-
"""
智联招聘 deviceSn — 纯协议生成/续期 (数美 reportShuMeiDevice)

实测: reportShuMeiDevice 接受任意 boxId/SMID 并返回 code=200 + 新 deviceSn
(智联不校验指纹绑定), 因此 deviceSn 可纯协议本地生成/续期, 无需数美 SDK。
(见 js_reverse_cache/tasks/zhilian-risk-phase0/report.md)
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

REPORT_URL = "https://cgate.zhaopin.com/userpassport/report/reportShuMeiDevice"

# 模拟浏览器请求头 (数美上报走 cgate)
REPORT_HEADERS = {
    "content-type": "application/json",
    "referer": "https://www.zhaopin.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}


def fetch_device_sn(client, box_id: str = "") -> Optional[str]:
    """
    纯协议获取/续期 deviceSn。

    Args:
        client: ZhilianClient 实例
        box_id: 数美 boxId (任意值即可, 智联不校验; 留空用 boxData)

    Returns:
        deviceSn 字符串; 失败返回 None
    """
    body = {"boxId": box_id} if box_id else {"boxData": ""}
    resp = client.post(REPORT_URL, headers=REPORT_HEADERS, json=body)
    if resp.status_code != 200:
        logger.warning("reportShuMeiDevice HTTP %s", resp.status_code)
        return None
    try:
        d = resp.json()
    except Exception as e:
        logger.warning("reportShuMeiDevice 响应非 JSON: %s", e)
        return None
    sn = (d.get("data") or {}).get("deviceSn")
    if not sn:
        logger.warning("reportShuMeiDevice 未返回 deviceSn: %s", str(d)[:120])
        return None
    logger.debug("获取 deviceSn: %s", sn[:8] + "...")
    return sn
