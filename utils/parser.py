# -*- coding: utf-8 -*-
"""
智联招聘搜索采集器 — SSR HTML 解析

职位数据在 SSR HTML 的 __INITIAL_STATE__ 内联 JSON 里 (positionList 字段)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

INITIAL_STATE_RE = re.compile(r"__INITIAL_STATE__=(\{.*?\})</script>", re.S)

# 输出字段映射: 标准字段名 -> __INITIAL_STATE__ positionList 里的源字段
FIELD_MAP = {
    "position_name": "name",
    "position_number": "number",
    "position_url": "positionUrl",
    "salary_display": "salary60",
    "salary_real": "salaryReal",
    "company_name": "companyName",
    "company_size": "companySize",
    "company_property": "propertyName",
    "company_number": "companyNumber",
    "city_id": "cityId",
    "city_district": "cityDistrict",
    "street_name": "streetName",
    "education": "education",
    "publish_time": "publishTime",
    "job_type": "subJobTypeLevelName",
    "work_exp": "workExp",
}


def extract_initial_state(html: str) -> Dict[str, Any]:
    """
    从 SSR HTML 提取 __INITIAL_STATE__ JSON 对象。

    Raises:
        ValueError: 未找到 __INITIAL_STATE__
    """
    m = INITIAL_STATE_RE.search(html)
    if not m:
        raise ValueError("HTML 中未找到 __INITIAL_STATE__")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"__INITIAL_STATE__ 不是合法 JSON: {e}") from e


def parse_positions(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """从 INITIAL_STATE 提取规范化职位记录列表。"""
    pl = state.get("positionList") or []
    rows: List[Dict[str, str]] = []
    for item in pl:
        if not isinstance(item, dict):
            continue
        row: Dict[str, str] = {}
        for out_key, src_key in FIELD_MAP.items():
            val = item.get(src_key)
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            row[out_key] = "" if val is None else str(val)
        # 技能标签
        skills = item.get("skillLabel") or item.get("showSkillTags") or []
        if skills:
            vals = []
            for s in skills:
                if isinstance(s, dict):
                    vals.append(str(s.get("value") or s.get("tag") or ""))
                else:
                    vals.append(str(s))
            row["skill_tags"] = "|".join([v for v in vals if v])
        else:
            row["skill_tags"] = ""
        rows.append(row)
    return rows


def parse_meta(state: Dict[str, Any]) -> Dict[str, Any]:
    """提取搜索元信息：总数、当前页、关键词等。"""
    return {
        "position_count": state.get("positionCount"),
        "page_index": state.get("pageIndex"),
        "page_size": state.get("pageSize"),
        "pages": state.get("pages"),
        "keywords": state.get("keyWords"),
        "query_params": state.get("queryParams"),
        "list_response_code": state.get("listResponseCode"),
        "search_condition": state.get("searchCondition"),
    }
