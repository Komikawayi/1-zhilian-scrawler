# -*- coding: utf-8 -*-
"""
智联招聘搜索采集器 — HTTP 客户端

核心：EdgeOne (腾讯云) 按 TLS/HTTP2 指纹判断是否放行。
用 curl_cffi impersonate=chrome 模拟 Chrome 指纹绕过。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

DEFAULT_IMPERSONATE = "chrome"

# 采集时每请求之间的最小/最大间隔（秒），风控友好
MIN_INTERVAL = 1.2
MAX_INTERVAL = 2.5

# 职位详情基础 UA（指纹模拟时 curl_cffi 会自动带 chrome 头，此处仅作兜底）
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class ZhilianClient:
    """智联搜索页纯协议客户端。"""

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        min_interval: float = MIN_INTERVAL,
        max_interval: float = MAX_INTERVAL,
        retries: int = 3,
        timeout: int = 25,
    ) -> None:
        self.impersonate = impersonate
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.retries = retries
        self.timeout = timeout
        self.session = cffi_requests.Session(impersonate=impersonate)
        self.session.headers.update(BASE_HEADERS)
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        """限速：每请求间随机等待。"""
        elapsed = time.monotonic() - self._last_request_at
        wait = random.uniform(self.min_interval, self.max_interval)
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

    def get(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        cookies: Optional[dict] = None,
        allow_redirects: bool = True,
    ) -> cffi_requests.Response:
        """
        统一限速 + 请求的公开入口 (供采集模块调用, 不暴露 session/私有字段)。

        Args:
            url: 请求 URL
            headers: 附加请求头 (与 BASE_HEADERS 合并)
            params: 查询参数
            cookies: 请求附加 cookie
            allow_redirects: 是否跟随重定向
        """
        self._throttle()
        return self.session.get(
            url,
            headers=headers,
            params=params,
            cookies=cookies,
            allow_redirects=allow_redirects,
            timeout=self.timeout,
        )

    def fetch_search_page(self, keyword: str, city: str, page: int = 1) -> str:
        """
        抓取搜索页 SSR HTML。

        sou.zhaopin.com 会 302 重定向到 www.zhaopin.com/sou/jl{kw编码}/p{page}，
        kw 编码在服务端完成，客户端无需自行实现。

        Returns:
            SSR HTML 文本
        """
        url = f"https://sou.zhaopin.com/?jl={city}&kw={keyword}&p={page}"
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                if resp.status_code != 200:
                    raise ConnectionError(f"HTTP {resp.status_code}")
                text = resp.text
                if "Security Verification" in text:
                    raise ConnectionError("EdgeOne 拦截 (Security Verification)")
                if "__INITIAL_STATE__" not in text:
                    raise ConnectionError("响应缺少 __INITIAL_STATE__")
                logger.debug("kw=%s city=%s page=%d -> %s (len=%d)", keyword, city, page, resp.url, len(text))
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("attempt %d/%d 失败 kw=%s p=%d: %s", attempt, self.retries, keyword, page, e)
                time.sleep(random.uniform(1.5, 4.0) * attempt)
        raise ConnectionError(f"抓取失败 kw={keyword} city={city} page={page}: {last_err}")
