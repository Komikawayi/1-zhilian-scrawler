# -*- coding: utf-8 -*-
"""
智联招聘采集器 — SSR / fe-api 数据解析

- 搜索: SSR __INITIAL_STATE__.positionList (parse_positions)
- 详情 SSR 路径: __INITIAL_STATE__.jobDetail (parse_job_detail)
- 详情 fe-api 路径: position-detailv2 响应 (parse_position_detail_v2)
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
        row = _pick(item, FIELD_MAP)
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


# ---- 职位详情页解析 ----

# detailedPosition 字段映射: 输出名 -> 源字段
DETAIL_POSITION_MAP = {
    "position_name": "positionName",
    "position_number": "positionNumber",
    "position_url": "positionUrl",
    "salary": "salary",
    "salary_type": "salaryType",
    "work_type": "workType",
    "working_exp": "positionWorkingExp",
    "education": "education",
    "recruit_number": "recruitNumber",
    "city_id": "positionCityId",
    "work_city": "positionWorkCity",
    "city_district": "positionCityDistrict",
    "publish_time": "positionPublishTime",
    "job_type": "jobTypeLevelName",
    "job_desc": "jobDesc",
    "work_address": "workAddress",
    "latitude": "latitude",
    "longitude": "longitude",
    "company_name": "companyName",
    "company_number": "companyNumber",
    "company_root_id": "companyRootId",
    "empl_type": "emplType",
    "job_status": "jobStatus",
    "position_highlight": "positionHighlight",
}

# detailedCompany 字段映射
DETAIL_COMPANY_MAP = {
    "company_name": "companyName",
    "company_number": "companyNumber",
    "company_size": "companySize",
    "company_description": "companyDescription",
    "company_url": "companyUrl",
    "company_logo": "companyLogo",
    "company_shot_name": "companyShotName",
    "financing_stage": "financingStageName",
    "industry_name": "industryNameLevel",
    "online_positions": "onlinePositionNumbers",
    "company_root_id": "companyRootId",
}


def _pick(obj: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, str]:
    """按映射取字段并规范化为字符串。"""
    row: Dict[str, str] = {}
    for out_key, src_key in mapping.items():
        val = obj.get(src_key)
        if isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        row[out_key] = "" if val is None else str(val)
    return row


def _parse_detail(
    dp: Dict[str, Any],
    dc: Dict[str, Any],
    position_map: Dict[str, str],
    welfare_src: str,
    id_key: str,
    id_val: Any,
) -> Dict[str, str]:
    """详情解析公共逻辑: 职位/公司映射 + 数组字段序列化 + 额外 id。

    Args:
        dp: detailedPosition 数据
        dc: detailedCompany 数据
        position_map: 职位字段映射 (SSR 用 DETAIL_POSITION_MAP, v2 用 V2_POSITION_MAP)
        welfare_src: 福利源字段 (SSR=welfareTags, v2=welfareLabel)
        id_key / id_val: 追加的 id 字段 (SSR=job_number, v2=task_id)
    """
    row = _pick(dp, position_map)
    row.update(_pick(dc, DETAIL_COMPANY_MAP))

    # 数组字段序列化 (v2 无 labels 时自动跳过)
    for key, src in (("welfare_tags", welfare_src), ("labels", "labels"), ("skill_tags", "skillLabel")):
        vals = dp.get(src) or []
        if vals:
            row[key] = "|".join(str(v) for v in vals)

    row[id_key] = str(id_val or "")
    return row


def parse_job_detail(state: Dict[str, Any]) -> Dict[str, str]:
    """
    从详情页 __INITIAL_STATE__ 提取规范化职位详情记录 (SSR 路径)。

    详情页结构: state.jobDetail = { detailedPosition, detailedCompany, taskId }
    """
    jd = state.get("jobDetail") or {}
    dp = jd.get("detailedPosition") or {}
    dc = jd.get("detailedCompany") or {}
    return _parse_detail(dp, dc, DETAIL_POSITION_MAP, "welfareTags",
                         id_key="job_number", id_val=state.get("jobNumber"))


# ---- position-detailv2 JSON API 解析 (无需 EdgeOne 挑战) ----

# v2 detailedPosition 字段: 输出名 -> 源字段 (注意 salary60/salaryReal/welfareLabel)
V2_POSITION_MAP = {
    "position_name": "positionName",
    "position_number": "positionNumber",
    "position_url": "positionUrl",
    "salary": "salary60",
    "salary_real": "salaryReal",
    "salary_type": "salaryType",
    "work_type": "workType",
    "working_exp": "positionWorkingExp",
    "education": "education",
    "recruit_number": "recruitNumber",
    "city_id": "positionCityId",
    "work_city": "positionWorkCity",
    "city_district": "positionCityDistrict",
    "publish_time": "positionPublishTime",
    "job_type": "jobTypeLevelName",
    "job_desc": "jobDesc",
    "work_address": "workAddress",
    "latitude": "latitude",
    "longitude": "longitude",
    "company_name": "companyName",
    "company_number": "companyNumber",
    "company_root_id": "companyRootId",
    "position_highlight": "positionHighlight",
    "rpo_proxy": "rpoProxy",
    "can_regular": "canBeRegular",
    "can_remote_internship": "canRemoteInternship",
}


def parse_position_detail_v2(data: Dict[str, Any]) -> Dict[str, str]:
    """
    解析 position-detailv2 API 响应 data 部分。

    结构: data = { detailedPosition, detailedCompany, taskId }
    纯协议可调, 无需 EdgeOne 挑战 (见 utils/fe_api.py fetch_position_detail_v2)。
    """
    dp = data.get("detailedPosition") or {}
    dc = data.get("detailedCompany") or {}
    return _parse_detail(dp, dc, V2_POSITION_MAP, "welfareLabel",
                         id_key="task_id", id_val=data.get("taskId"))
