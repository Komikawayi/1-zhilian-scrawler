# -*- coding: utf-8 -*-
"""
智联招聘登录会话管理 — at/rt 令牌加载与保存

登录态经 URL 查询参数 at(access token) + rt(refresh token) 传递,
纯协议附加到 fe-api 请求即可访问登录态接口 (见 js_reverse_cache/tasks/zhilian-risk-phase0/report.md)。
"""
from __future__ import annotations

import json
import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "zhilian-session.local.json",
)


def current_machine() -> dict:
    """当前机器标识 (hostname + 出口 IP)。用于区分多台开发机的会话配置。"""
    host = socket.gethostname()
    ip = ""
    try:
        # 出口 IP 从会话文件历史/网络推断, 这里取本机局域网 IP 作为兜底
        ip = socket.gethostbyname(host)
    except OSError:
        pass
    return {"hostname": host, "ip": ip, "captured_by": "zhilian-crawler"}


class Session:
    """登录会话: 持 at/rt 令牌, 提供 fe-api 认证参数。"""

    def __init__(self, at: str, rt: str, source: str = ""):
        self.at = at
        self.rt = rt
        self.source = source

    def auth_params(self) -> dict:
        """返回注入请求的登录态参数 (at/rt)。"""
        return {"at": self.at, "rt": self.rt}

    def is_loaded(self) -> bool:
        return bool(self.at and self.rt)


def load_session(path: Optional[str] = None) -> Optional[Session]:
    """
    从会话文件加载 at/rt。

    Args:
        path: 会话文件路径 (默认 config/zhilian-session.local.json)

    Returns:
        Session; 文件缺失/格式错返回 None
    """
    path = path or DEFAULT_SESSION_PATH
    if not os.path.exists(path):
        logger.warning("会话文件不存在: %s (请先捕获登录会话)", path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        at, rt = data.get("at", ""), data.get("rt", "")
        if not at or not rt:
            logger.warning("会话文件缺少 at/rt: %s", path)
            return None
        # 机器区分: 若会话文件记录了来源机器, 且与当前机器不同, 告警
        m = data.get("machine") or {}
        if m.get("hostname") and m["hostname"] != current_machine()["hostname"]:
            logger.warning(
                "会话文件来自另一台机器 %s (当前 %s): %s — 请确认是否本机采集",
                m["hostname"], current_machine()["hostname"], path,
            )
        return Session(at, rt, source=data.get("source", path))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("会话文件读取失败 %s: %s", path, e)
        return None


def save_session(at: str, rt: str, path: Optional[str] = None, source: str = "") -> str:
    """保存 at/rt 到会话文件 (默认 config/zhilian-session.local.json)。

    自动附带当前机器标识 (machine.hostname), 便于多台开发机区分配置。
    """
    path = path or DEFAULT_SESSION_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"at": at, "rt": rt, "source": source, "machine": current_machine()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("会话已保存: %s", path)
    return path
