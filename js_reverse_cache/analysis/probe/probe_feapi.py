# -*- coding: utf-8 -*-
"""
Phase 1 验证: fe-api 接口能否用 curl_cffi 纯协议调用 (无需浏览器/瑞数)

测试目标:
  1. search/base/data       - 筛选字典 (浏览器: code=200)
  2. city-page/user-city    - 城市定位
  3. experiment/config/initialize - 实验配置
  4. user/unread-message    - 未读消息
  5. SSR 搜索页 (对照组, 已知 curl_cffi 可通过)
"""
import json
import sys
import time
import uuid

from curl_cffi import requests as cffi_requests

BASE = "https://fe-api.zhaopin.com/c/i"

def xzp_headers(extra=None):
    h = {
        "accept": "application/json, text/plain, */*",
        "x-zp-business-system": "1",
        "x-zp-page-code": "4019",
        "x-zp-platform": "13",
        "referer": "https://www.zhaopin.com/",
    }
    if extra:
        h.update(extra)
    return h


def build_common_params():
    # 复刻浏览器参数形态
    _v = "%.8f" % (time.time() % 1)          # 随机小数
    req_id = uuid.uuid4().hex + "-" + str(int(time.time() * 1000)) + "-" + str(time.time()).split(".")[1][:6]
    client_id = str(uuid.uuid4())
    return {"_v": _v, "x-zp-page-request-id": req_id, "x-zp-client-id": client_id}


def probe(name, method, url, params=None, **kw):
    t0 = time.time()
    try:
        resp = cffi_requests.request(
            method, url, params=params, headers=xzp_headers(),
            impersonate="chrome", timeout=25, **kw,
        )
        dt = time.time() - t0
        body = resp.text
        code = None
        try:
            code = resp.json().get("code")
        except Exception:
            pass
        print(f"[{name}] status={resp.status_code} code={code} len={len(body)} t={dt:.1f}s url={url.split('/c/i/')[-1][:60]}")
        return resp
    except Exception as e:
        print(f"[{name}] ERROR {type(e).__name__}: {str(e)[:150]}")
        return None


def main():
    print("== 1. SSR 搜索页对照组 ==")
    r = probe("ssr-search", "GET",
              "https://sou.zhaopin.com/?jl=530&kw=python&p=1",
              allow_redirects=True)
    if r is not None:
        print("   拦截?" , "Security Verification" in r.text)

    print("\n== 2. fe-api 接口 (随机参数) ==")
    probe("search/base/data", "GET", f"{BASE}/search/base/data", build_common_params())
    probe("user-city", "GET", f"{BASE}/city-page/user-city",
          {**build_common_params(), "ipCity": "杭州", "ipProvince": "浙江", "userDesiredCity": ""})
    probe("experiment", "GET", f"{BASE}/experiment/config/initialize", build_common_params())
    probe("unread-message", "GET", f"{BASE}/user/unread-message", build_common_params())

    print("\n== 3. fe-api 接口 (浏览器抓到的真实参数) ==")
    real = {
        "_v": "0.42783300",
        "x-zp-page-request-id": "52051d44c0b3403bb18e69d25d3b38c0-1785943874648-484949",
        "x-zp-client-id": "477c36a2-f288-46b2-afde-838ed9f38fbb",
    }
    probe("search/base/data[real]", "GET", f"{BASE}/search/base/data", real)

    print("\n== 4. fe-api 完全无参数 ==")
    probe("search/base/data[no-param]", "GET", f"{BASE}/search/base/data")


if __name__ == "__main__":
    main()
