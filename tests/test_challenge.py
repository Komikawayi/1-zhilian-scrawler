# -*- coding: utf-8 -*-
"""
固定输入自检: EdgeOne JS Challenge 求解 + 详情页解析

不依赖网络。challenge 样本来自已保存的真实响应, 详情 state 来自已保存的真实数据。
"""
import json
import os
import shutil
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from utils.challenge import is_challenge_html, is_captcha_page, solve_challenge_js, extract_script  # noqa: E402
from utils.parser import extract_initial_state, parse_job_detail, parse_position_detail_v2  # noqa: E402

CACHE = os.path.join(BASE, "js_reverse_cache")
FIXTURES = os.path.join(BASE, "tests", "fixtures")


def _read(rel: str) -> str:
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


# ---- 分类 ----

def test_classify_challenge():
    html = _read("js_reverse_cache/assets/html/jobdetail_shell.html")
    assert is_challenge_html(html) is True
    assert is_captcha_page(html) is False


def test_classify_captcha():
    captcha = """<html><title>Security Verification</title>
    <script src="https://captcha.eo.qq.com/cap_union_prehandle?..."></script>
    正在验证连接安全性</html>"""
    assert is_captcha_page(captcha) is True
    assert is_challenge_html(captcha) is False


def test_extract_script():
    html = _read("js_reverse_cache/assets/html/jobdetail_shell.html")
    script = extract_script(html)
    assert script is not None
    assert "solveChallenge" in script
    assert "EO-Bot-Js-Token" in script


# ---- Node challenge 求解 (固定输入) ----

@pytest.mark.skipif(shutil.which("node") is None, reason="Node 不可用")
def test_solve_challenge_fixed():
    """用固定 challenge 样本求解, 必须返回非空 token。"""
    script = _read("js_reverse_cache/assets/js/eo_challenge_0.js")
    token = solve_challenge_js(script)
    assert token, "challenge 求解返回空"
    assert token.startswith("local#"), f"token 格式异常: {token[:20]}"
    assert len(token) > 200


# ---- 详情页解析 (固定 fixture) ----

def test_extract_initial_state_from_detail():
    html = _read("js_reverse_cache/assets/html/detail_data_0.html")
    state = extract_initial_state(html)
    assert "jobDetail" in state
    assert "detailedPosition" in state["jobDetail"]


def test_parse_job_detail_fixed():
    with open(os.path.join(FIXTURES, "detail_state.json"), encoding="utf-8") as f:
        state = json.load(f)
    detail = parse_job_detail(state)
    assert detail["position_name"] == "java/c++/C/Python/前端开发工程师"
    assert "1.5-3万" in detail["salary"]
    assert detail["education"] == "本科"
    assert detail["company_name"] == "外企德科数字技术有限公司"
    assert detail["company_size"] == "10000人以上"
    assert "岗位职责" in detail["job_desc"]
    assert "海淀区" in detail["city_district"]


# ---- position-detailv2 JSON API 解析 (固定 fixture) ----

def test_parse_position_detail_v2_fixed():
    """用真实 position-detailv2 响应验证解析 (无需挑战的推荐路径)。"""
    with open(os.path.join(CACHE, "assets", "network", "position_detailv2.json"), encoding="utf-8") as f:
        resp = json.load(f)
    assert resp["code"] == 200 and resp["apiCode"] == 200
    detail = parse_position_detail_v2(resp["data"])
    assert detail["position_name"] == "java/c++/C/Python/前端开发工程师"
    assert "1.5-3万" in detail["salary"]
    assert detail["education"] == "本科"
    assert detail["work_city"] == "北京"
    assert "海淀区" in detail["city_district"]
    assert detail["company_name"] == "外企德科数字技术有限公司"
    assert "岗位职责" in detail["job_desc"]
    assert len(detail["job_desc"]) > 200  # 完整职位描述
