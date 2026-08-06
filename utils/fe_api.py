# -*- coding: utf-8 -*-
"""
智联招聘 fe-api JSON 接口 — 纯协议请求 (无需 EdgeOne 挑战/签名)

动态参数 (_v / x-zp-page-request-id / x-zp-client-id) 服务端不校验, 随机生成即可。

两类接口:
- 匿名接口: position-detailv2 职位详情等, 实测 20/20 稳定, 无挑战/验证码/IP 信誉依赖
- 登录态接口: 需 URL 参数 at/rt (Session), 简历/消息/投递/VIP 等

登录态经 URL 参数 at(access token)+rt(refresh token) 传递 (见 utils/session.py)。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Dict, Optional

from utils.session import Session

logger = logging.getLogger(__name__)

# fe-api 固定请求头
FE_API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-zp-business-system": "1",
    "x-zp-page-code": "4019",
    "x-zp-platform": "13",
    "referer": "https://www.zhaopin.com/",
}

BASE_URL = "https://fe-api.zhaopin.com"


class LoginExpiredError(ConnectionError):
    """登录会话过期 (fe-api code=210 未登录), 需重新捕获 at/rt。"""


def _fe_api_params(session: Optional[Session] = None, **extra) -> dict:
    """构造 fe-api 动态参数 (非签名, 随机即可); session 非空时注入 at/rt。"""
    params = {
        "_v": "%.8f" % (time.time() % 1),
        "x-zp-page-request-id": uuid.uuid4().hex + "-" + str(int(time.time() * 1000)),
        "x-zp-client-id": str(uuid.uuid4()),
        "platform": "13",
        "version": "0.0.0",
    }
    if session and session.is_loaded():
        params.update(session.auth_params())
    params.update(extra)
    return params


def _check_ok(body: Dict, name: str) -> None:
    """校验 fe-api 通用响应码, 非 200 抛 ConnectionError。"""
    if body.get("code") != 200 or body.get("apiCode") != 200:
        raise ConnectionError(
            f"{name} 业务错误 code={body.get('code')} apiCode={body.get('apiCode')} msg={body.get('message')}"
        )


def _fetch_json(
    client,
    path: str,
    session: Optional[Session] = None,
    method: str = "GET",
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    name: str = "",
) -> Dict:
    """通用 fe-api 请求, 返回响应 JSON。"""
    url = BASE_URL + path
    extra = params or {}
    if method.upper() == "POST":
        resp = client.post(url, headers=FE_API_HEADERS, params=_fe_api_params(session, **extra), json=body)
    else:
        resp = client.get(url, headers=FE_API_HEADERS, params=_fe_api_params(session, **extra))
    if resp.status_code != 200:
        raise ConnectionError(f"{name or path} HTTP {resp.status_code}")
    try:
        body = resp.json()
    except json.JSONDecodeError as e:
        raise ConnectionError(f"{name or path} 响应非 JSON: {e}") from e
    if body.get("code") == 210:
        raise LoginExpiredError(f"{name or path} 会话过期 (code=210 未登录), 需重新捕获 at/rt")
    return body


# ---- 匿名接口 ----

DETAIL_V2_URL = BASE_URL + "/c/i/jobs/position-detailv2"


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
    except json.JSONDecodeError as e:
        raise ConnectionError(f"position-detailv2 响应非 JSON: {e}") from e
    _check_ok(body, "position-detailv2")
    return body.get("data") or {}


# ---- 登录态接口 (需 Session at/rt) ----

def fetch_unread_message(client, session: Session) -> Dict:
    """未读消息数。匿名 code=210, 登录态 code=200。"""
    body = _fetch_json(client, "/c/i/user/unread-message", session, name="unread-message")
    _check_ok(body, "unread-message")
    return body.get("data")


def fetch_resume_list(client, session: Session) -> Dict:
    """简历列表 (含简历 id/number/完整度)。"""
    body = _fetch_json(client, "/c/i/resumelist-selectable", session, name="resumelist")
    _check_ok(body, "resumelist-selectable")
    return body.get("data")


def fetch_resume_feedback_count(client, session: Session) -> Dict:
    """简历反馈统计 (投递数/面试邀请数/面试数)。"""
    body = _fetch_json(client, "/c/i/resume/feedback/get-count", session, name="resume-feedback")
    _check_ok(body, "resume-feedback")
    return body.get("data")


def fetch_vip_info(client, session: Session) -> Dict:
    """VIP 信息。"""
    body = _fetch_json(client, "/c/i/business/vip-info", session, name="vip-info")
    return body.get("data")


def fetch_navigation_list(client, session: Session) -> Dict:
    """导航列表。"""
    body = _fetch_json(client, "/c/i/navigation-list", session, name="navigation-list")
    _check_ok(body, "navigation-list")
    return body.get("data")


def fetch_resume_diagnosis(client, session: Session, resume_number: str, resume_id: str) -> Dict:
    """简历诊断。"""
    body = _fetch_json(
        client, "/c/i/resume/diagnosis/number", session,
        params={"resumeNumber": resume_number, "resumeId": resume_id, "resumeLanguage": 1},
        name="resume-diagnosis",
    )
    _check_ok(body, "resume-diagnosis")
    return body.get("data")
