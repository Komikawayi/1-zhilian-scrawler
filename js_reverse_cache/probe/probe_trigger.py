# -*- coding: utf-8 -*-
"""
触发条件探测: 连续访问职位详情页, 观察 EdgeOne 防线升级
  - 真实数据  (~1.7MB, __INITIAL_STATE__)
  - JS challenge (29KB, solveChallenge)
  - 交互验证码  (Security Verification / cap_union_prehandle)
"""
import time
from curl_cffi import requests as cr

URL = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm"
N = 15
SLEEP = 0.4


def classify(t):
    if "Security Verification" in t and "EO-Bot" not in t:
        return "交互验证码(轻)"
    if "solveChallenge" in t:
        return "JS-challenge"
    if "__INITIAL_STATE__" in t:
        return "真实数据"
    return "其他(%dB)" % len(t)


def main():
    print(f"== 探测: 连续 {N} 次访问详情页 (间隔 {SLEEP}s) ==")
    for i in range(1, N + 1):
        t0 = time.time()
        try:
            r = cr.get(URL, impersonate="chrome", timeout=25, allow_redirects=True)
            dt = time.time() - t0
            print(f"[{i:2d}] {classify(r.text):<20} status={r.status_code} len={len(r.text):>8} t={dt:.1f}s")
        except Exception as e:
            print(f"[{i:2d}] ERROR {type(e).__name__}: {str(e)[:100]}")
        time.sleep(SLEEP)


if __name__ == "__main__":
    main()
