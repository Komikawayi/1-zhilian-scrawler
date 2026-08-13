# -*- coding: utf-8 -*-
"""Synthetic challenge and detail parsing tests that do not require captured traffic."""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from utils.challenge import extract_script, is_captcha_page, is_challenge_html, solve_challenge_js  # noqa: E402
from utils.parser import extract_initial_state, parse_job_detail, parse_position_detail_v2  # noqa: E402

CHALLENGE_HTML = "<script>function solveChallenge() {} // EO-Bot-Js-Token</script>"
DETAIL_STATE = {
    "jobNumber": "sample-job",
    "jobDetail": {
        "detailedPosition": {
            "positionName": "Sample Engineer",
            "salary": "10k-15k",
            "education": "Bachelor",
            "positionCityDistrict": "Sample District",
            "jobDesc": "Responsibilities<br>Requirements",
        },
        "detailedCompany": {
            "companyName": "Sample Company",
            "companySize": "100-499",
        },
    },
}
DETAIL_HTML = "<script>__INITIAL_STATE__=" + json.dumps(DETAIL_STATE) + "</script>"
V2_DETAIL = {
    "detailedPosition": {
        "positionName": "Sample Engineer",
        "salary60": "10k-15k",
        "education": "Bachelor",
        "positionWorkCity": "Sample City",
        "positionCityDistrict": "Sample District",
        "jobDesc": "Responsibilities<br>Requirements",
    },
    "detailedCompany": {"companyName": "Sample Company"},
    "taskId": "sample-task",
}


def test_classify_challenge():
    assert is_challenge_html(CHALLENGE_HTML) is True
    assert is_captcha_page(CHALLENGE_HTML) is False


def test_classify_captcha():
    captcha = "<html><title>Security Verification</title></html>"
    assert is_captcha_page(captcha) is True
    assert is_challenge_html(captcha) is False


def test_extract_script():
    script = extract_script(CHALLENGE_HTML)
    assert script is not None
    assert "solveChallenge" in script
    assert "EO-Bot-Js-Token" in script


def test_solve_challenge_rejects_invalid_script():
    assert solve_challenge_js("not valid javascript") is None


def test_extract_initial_state_from_detail():
    state = extract_initial_state(DETAIL_HTML)
    assert "jobDetail" in state
    assert "detailedPosition" in state["jobDetail"]


def test_parse_job_detail_fixed():
    detail = parse_job_detail(DETAIL_STATE)
    assert detail["position_name"] == "Sample Engineer"
    assert detail["salary"] == "10k-15k"
    assert detail["education"] == "Bachelor"
    assert detail["company_name"] == "Sample Company"
    assert detail["company_size"] == "100-499"
    assert detail["job_desc"] == "Responsibilities\nRequirements"
    assert detail["city_district"] == "Sample District"


def test_parse_position_detail_v2_fixed():
    detail = parse_position_detail_v2(V2_DETAIL)
    assert detail["position_name"] == "Sample Engineer"
    assert detail["salary"] == "10k-15k"
    assert detail["education"] == "Bachelor"
    assert detail["work_city"] == "Sample City"
    assert detail["city_district"] == "Sample District"
    assert detail["company_name"] == "Sample Company"
    assert detail["job_desc"] == "Responsibilities\nRequirements"
