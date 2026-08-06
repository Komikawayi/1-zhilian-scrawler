# -*- coding: utf-8 -*-
"""重放变体对照: 找出能拿到真实数据的重放方式 (IP 已冷却)"""
import json
import os
import re
import subprocess
import tempfile
import time

from curl_cffi import requests as cr

URL = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm"


def solve(html):
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    fd, tmp = tempfile.mkstemp(suffix=".js")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(m.group(1))
    p = subprocess.run(["node", "tools/eo_solve.js", tmp], capture_output=True, text=True, timeout=30)
    os.remove(tmp)
    return json.loads(p.stdout).get("token")


def classify(t):
    if "__INITIAL_STATE__" in t:
        return "✅数据"
    if "Security Verification" in t:
        return "⚠️验证码"
    if "solveChallenge" in t:
        return "🔄challenge"
    return f"❓{len(t)}B"


def main():
    print("== 变体 A: 顶层 cr.get (无 session) ==")
    r = cr.get(URL, impersonate="chrome", timeout=25)
    print(f"  首跳: {classify(r.text)}")
    if "solveChallenge" in r.text:
        token = solve(r.text)
        for i in range(2):
            r2 = cr.get(URL, impersonate="chrome", timeout=25, cookies={"EO-Bot-Js-Token": token})
            print(f"  重放#{i}: {classify(r2.text)}")
            time.sleep(1)

    print("\n== 变体 B: 全新 Session (无 cookie 污染) ==")
    s = cr.Session(impersonate="chrome", timeout=25)
    r = s.get(URL)
    print(f"  首跳: {classify(r.text)}")
    if "solveChallenge" in r.text:
        token = solve(r.text)
        # 清掉 session 里的错误 cookie
        s.cookies.clear()
        r2 = s.get(URL, cookies={"EO-Bot-Js-Token": token})
        print(f"  重放(清cookie): {classify(r2.text)}")

    print("\n== 变体 C: Session 但不清 cookie ==")
    s2 = cr.Session(impersonate="chrome", timeout=25)
    r = s2.get(URL)
    if "solveChallenge" in r.text:
        token = solve(r.text)
        r2 = s2.get(URL, cookies={"EO-Bot-Js-Token": token})
        print(f"  首跳: {classify(r.text)} -> 重放(带错误cookie): {classify(r2.text)}")
        print(f"  session cookie jar: {[(c.name, c.value[:20]) for c in s2.cookies]}")


if __name__ == "__main__":
    main()
