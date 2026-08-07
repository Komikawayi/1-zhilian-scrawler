# -*- coding: utf-8 -*-
"""
requests 版详情页 challenge 流程验证:
  用原生 requests (cookie 解析正确) 走 首跳 -> challenge -> 求解 -> 带完整cookie重放
对比 curl_cffi 只带 token 的重放, 确认 acw_tc/cdn_sec_tc 会话 cookie 是否为重放必需。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests
from curl_cffi import requests as cr

URL = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"


def solve(html):
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    fd, tmp = tempfile.mkstemp(suffix=".js")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(m.group(1))
    p = subprocess.run(["node", "tools/eo_solve.js", tmp], capture_output=True, text=True, timeout=30)
    os.remove(tmp)
    return json.loads(p.stdout).get("token")


def main():
    print("===== A. requests Session (HTTP/1.1, 完整 cookie) =====")
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.headers["Accept-Language"] = "zh-CN,zh;q=0.9"
    for i in range(3):
        r = s.get(URL, timeout=25)
        print(f"  [{i}] len={len(r.text)} challenge={'solveChallenge' in r.text} 数据={'__INITIAL_STATE__' in r.text} 验证码={'Security Verification' in r.text}")
        if "solveChallenge" in r.text:
            token = solve(r.text)
            print(f"  token={token[:25]}... len={len(token)}")
            # 带 session 完整 cookie 重放 (requests 自动带 acw_tc 等)
            r2 = s.get(URL, timeout=25)
            t2 = r2.text
            print(f"  重放(len via session cookies): len={len(t2)} 数据={'__INITIAL_STATE__' in t2} 验证码={'Security Verification' in t2}")
            # 额外带 token cookie
            s.cookies.set("EO-Bot-Js-Token", token, domain="www.zhaopin.com", path="/")
            r3 = s.get(URL, timeout=25)
            t3 = r3.text
            print(f"  重放(+token): len={len(t3)} 数据={'__INITIAL_STATE__' in t3} 验证码={'Security Verification' in t3}")
        time.sleep(1.5)

    print("\n===== B. curl_cffi Session 对比 =====")
    cs = cr.Session(impersonate="chrome", timeout=25)
    for i in range(2):
        r = cs.get(URL)
        print(f"  [{i}] len={len(r.text)} challenge={'solveChallenge' in r.text}")
        if "solveChallenge" in r.text:
            token = solve(r.text)
            # curl_cffi 带 token + 手动 acw_tc (用 requests 拿到的)
            r2 = cs.get(URL, cookies={"EO-Bot-Js-Token": token})
            t2 = r2.text
            print(f"  重放(+token): len={len(t2)} 数据={'__INITIAL_STATE__' in t2} 验证码={'Security Verification' in t2}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
