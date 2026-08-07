# -*- coding: utf-8 -*-
"""
智联招聘职位详情页 SSR — EdgeOne JS Challenge 求解 + 抓取 (兜底路径)

推荐路径: 见 utils/fe_api.py (position-detailv2 JSON API, 无需挑战)。

原理 (server-js-cookie-bootstrap):
  1. GET /jobdetail/{id}.htm (无 cookie) -> 29KB JS Challenge 壳
  2. 壳内含 91-opcode 字节码 VM, 执行后定义 window.solveChallenge(challenge, seed)
  3. Node vm 最小沙箱执行 challenge JS -> 得到 EO-Bot-Js-Token
  4. 带 cookie 重放 -> 拿到含 __INITIAL_STATE__ 的真实 SSR 数据

curl_cffi (Chrome TLS 指纹) 稳定触发 JS Challenge 层;
TCaptcha 交互验证码仅在 IP 信誉极差时触发 (见 js_reverse_cache/tasks/*/task.json)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

logger = logging.getLogger(__name__)

# challenge 壳特征
CHALLENGE_MARKERS = ("solveChallenge", "EO-Bot-Js-Token")

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

# Node 执行器路径 (相对本文件: utils/../tools/eo_solve.js)
EO_SOLVE_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "eo_solve.js",
)


def is_challenge_html(html: str) -> bool:
    """判断是否 EdgeOne JS Challenge 壳 (而非真实数据/验证码)。"""
    return all(m in html for m in CHALLENGE_MARKERS) and "__INITIAL_STATE__" not in html


def is_captcha_page(html: str) -> bool:
    """判断是否触发交互验证码 (极端兜底, 需冷却)。

    注意: 轻量验证码页 (<title>Security Verification</title>) 可能不含
    cap_union_prehandle 引用, 只检查标题即可。
    """
    return "Security Verification" in html


def extract_script(html: str) -> Optional[str]:
    """提取 challenge HTML 中第一个内联 <script> 内容。"""
    m = SCRIPT_RE.search(html)
    return m.group(1) if m else None


def solve_challenge_js(script: str, href: Optional[str] = None, timeout: int = 30) -> Optional[str]:
    """
    在 Node vm 最小沙箱执行 challenge JS, 返回 EO-Bot-Js-Token。

    Args:
        script: challenge 内联 JS 源码
        href: 当前请求 URL (传给沙箱 location.href, 与请求上下文保持一致)
        timeout: Node 执行超时 (秒)

    Returns:
        token 字符串; 失败返回 None
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".js", prefix="eo_ch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        cmd = ["node", EO_SOLVE_JS, tmp_path] + ([href] if href else [])
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            logger.error("Node challenge 求解失败: %s", proc.stderr[:200])
            return None
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.error("Node 输出非 JSON: %s", proc.stdout[:200])
            return None
        token = result.get("token")
        if not token:
            logger.error("Node 未返回 token: %s", result)
            return None
        logger.debug("challenge 求解成功 token_len=%d", len(token))
        return token
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error("Node 执行异常: %s", e)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _handle_captcha(cooldown: float, is_last: bool) -> bool:
    """交互验证码分支: 非最后一次且设了冷却则等待继续, 否则抛错。

    Returns:
        True 表示已冷却, 调用方应 continue 重试
    """
    if cooldown > 0 and not is_last:
        logger.warning("触发 EdgeOne 交互验证码, 冷却 %.0fs 后重试", cooldown)
        time.sleep(cooldown)
        return True
    raise ConnectionError(
        "触发 EdgeOne 交互验证码 (Security Verification), IP 需冷却或降低频率"
    )


def fetch_job_detail(
    client,
    position_url: str,
    retries: int = 2,
    captcha_cooldown: float = 0.0,
    risk=None,
) -> str:
    """
    抓取职位详情页 SSR HTML, 自动处理 EdgeOne JS Challenge (兜底路径)。

    Args:
        client: ZhilianClient 实例
        position_url: 职位详情 URL (如 http://www.zhaopin.com/jobdetail/{id}.htm)
        retries: challenge 求解重试次数 (实际尝试 retries+1 次)
        captcha_cooldown: CLI 兜底的冷却秒数 (有 risk 状态机时以状态机冷却为准)
        risk: RiskState 实例 (可选) — token 缓存复用 + 防线上报

    Returns:
        含 __INITIAL_STATE__ 的真实 SSR HTML

    Raises:
        ConnectionError: 反复触发 challenge / 交互验证码 / 抓取失败
    """
    # positionUrl 常为 http://, 统一为 https
    if position_url.startswith("http://"):
        position_url = "https://" + position_url[len("http://"):]

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        is_last = attempt == retries
        resp = client.get(position_url)
        if resp.status_code != 200:
            raise ConnectionError(f"HTTP {resp.status_code}")
        html = resp.text

        # 拿到真实数据
        if "__INITIAL_STATE__" in html and "jobDetail" in html:
            client.report_success()
            return html

        # 交互验证码 (IP 信誉恶化) -> 冷却后重试或抛错
        if is_captcha_page(html):
            client.report_captcha()
            if risk is not None:
                risk.await_cooldown()
                if not is_last:
                    continue
            if _handle_captcha(captcha_cooldown, is_last):
                continue

        # JS Challenge -> 缓存复用或本地求解 -> 带 cookie 重放
        if is_challenge_html(html):
            client.report_challenge()
            token = risk.get_token(position_url) if risk is not None else None
            if not token:
                script = extract_script(html)
                if not script:
                    raise ConnectionError("challenge 壳中未找到内联 script")
                token = solve_challenge_js(script, href=position_url)
                if not token:
                    last_err = ConnectionError("challenge JS 求解失败")
                    continue
                if risk is not None:
                    risk.set_token(position_url, token)
                    risk.save()
            logger.debug("详情页 attempt=%d 使用 EO-Bot-Js-Token (len=%d), 重放", attempt, len(token))
            resp2 = client.get(position_url, cookies={"EO-Bot-Js-Token": token})
            if resp2.status_code != 200:
                last_err = ConnectionError(f"重放 HTTP {resp2.status_code}")
                continue
            html2 = resp2.text
            if "__INITIAL_STATE__" in html2 and "jobDetail" in html2:
                client.report_success()
                return html2
            if is_captcha_page(html2):
                client.report_captcha()
                if risk is not None:
                    risk.await_cooldown()
                    if not is_last:
                        continue
                if _handle_captcha(captcha_cooldown, is_last):
                    continue
            last_err = ConnectionError(
                f"重放后仍未拿到数据 (len={len(html2)}, challenge={is_challenge_html(html2)})"
            )
            continue

        # 其他未知形态
        last_err = ConnectionError(f"详情页未知响应形态 len={len(html)}")
        if not is_last:
            logger.warning("详情页 attempt %d 未知形态: %s", attempt, last_err)

    raise ConnectionError(f"详情页抓取失败 {position_url}: {last_err}")
