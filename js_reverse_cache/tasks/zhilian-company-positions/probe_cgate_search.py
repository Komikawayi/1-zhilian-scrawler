# -*- coding: utf-8 -*-
"""
cgate searchPositionsCompany 纯协议探测 — 公司在招职位列表

目标: 验证 cgate.zhaopin.com 域名接口能否脱离浏览器纯协议调用。
对照主线 position-detailv2 (fe-api 匿名 20/20) 的机制差异:
  - 域名不同: cgate.zhaopin.com vs fe-api.zhaopin.com (风控可能更严)
  - POST + JSON body (vs fe-api 的 GET params)
  - at/rt 从 .zhaopin.com 域 cookie 读取注入 URL params (axios 拦截器)
  - withCredentials=true

三种形态对比:
  A. 匿名           无 at/rt            -> 判断接口是否匿名可达
  B. at/rt 注入     at/rt 放 URL params -> 复刻 axios 拦截器注入
  C. 完整 cookie    浏览器 cookie        -> 对照组, 看是否还缺其它 cookie/header
"""
import json
import os
import sys
import time

from curl_cffi import requests as cffi_requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from utils.session import load_session  # noqa: E402

CGATE_URL = "https://cgate.zhaopin.com/positionbusiness/searchrecommend/searchPositionsCompany"
COMPANY_ID = "146264427"  # 测试公司号 (从 detail fixture 取)

# cgate 固定请求头 (复刻浏览器, 未知字段待实测调优)
CGATE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.zhaopin.com",
    "referer": "https://www.zhaopin.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def build_body(page_index=1, page_size=20):
    """复刻 companydetail.web.js 调用方参数 (S_SOU_* 硬编码字段名)。"""
    return {
        "S_SOU_COMPANY_ID": COMPANY_ID,
        "pageSize": page_size,
        "pageIndex": page_index,
        # S_SOU_SALARY_MIN/MAX 在调用方为 "R,R" (范围重复值), 空档用空串
        "S_SOU_SALARY_MIN": "",
        "S_SOU_SALARY_MAX": "",
        "S_SOU_WORK_CITY": "",
        "S_SOU_JD_JOB_LEVEL": "",
    }


def build_params(at="", rt=""):
    """复刻 axios 拦截器注入: at/rt + _v 随机 + platform=13。"""
    p = {"_v": str(round(time.time() % 1, 8))}
    if at:
        p["at"] = at
    if rt:
        p["rt"] = rt
    return p


def summarize(body):
    """打印响应结构摘要。"""
    if not isinstance(body, dict):
        return f"non-dict: {type(body).__name__} len={len(body) if hasattr(body,'__len__') else '?'}"
    code = body.get("code")
    api = body.get("apiCode")
    msg = body.get("message")
    data = body.get("data")
    out = [f"code={code} apiCode={api} msg={msg!r}"]
    if isinstance(data, dict):
        out.append(f"data.keys={list(data.keys())[:12]}")
        for k in ("pageIndex", "pageSize", "totalCount", "totalPage", "positionList", "list", "records"):
            if k in data:
                v = data[k]
                out.append(f"  {k}={len(v) if isinstance(v, (list, dict)) else v}")
                if k in ("positionList", "list", "records") and isinstance(v, list) and v:
                    out.append(f"    first_item_keys={list(v[0].keys())[:15]}")
    return "\n".join(out)


def probe(name, at="", rt="", cookies=None, headers=None):
    t0 = time.time()
    try:
        resp = cffi_requests.post(
            CGATE_URL,
            headers=headers or CGATE_HEADERS,
            params=build_params(at, rt),
            json=build_body(),
            cookies=cookies,
            impersonate="chrome",
            timeout=25,
        )
        dt = time.time() - t0
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        print(f"\n[{name}] status={resp.status_code} t={dt:.1f}s len={len(resp.text)}")
        print(f"  {summarize(body) if isinstance(body, dict) else body[:300]}")
        return resp
    except Exception as e:  # noqa: BLE001
        print(f"\n[{name}] ERROR {type(e).__name__}: {str(e)[:200]}")
        return None


def main():
    print(f"目标: {CGATE_URL}\n公司号: {COMPANY_ID}\n")

    print("=" * 60)
    print("A. 匿名 (无 at/rt)")
    print("=" * 60)
    probe("anonymous")

    print("\n" + "=" * 60)
    print("B. at/rt 注入 (URL params, 复刻 axios 拦截器)")
    print("=" * 60)
    sess = load_session()
    if sess and sess.is_loaded():
        probe("at-rt", at=sess.at, rt=sess.rt)
    else:
        print("!! 无可用 session (config/zhilian-session.local.json 缺失), 跳过 B")

    print("\n" + "=" * 60)
    print("C. 完整 cookie (浏览器捕获, 对照组)")
    print("=" * 60)
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.json")
    if os.path.exists(cookie_file):
        cookies = json.load(open(cookie_file, encoding="utf-8"))
        probe("full-cookie", cookies=cookies)
    else:
        print(f"!! 无 {cookie_file}, 跳过 C (可从浏览器复制 cookie 到此文件再跑)")


if __name__ == "__main__":
    main()
