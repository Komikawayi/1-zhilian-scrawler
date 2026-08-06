# -*- coding: utf-8 -*-
"""
端到端验证: 职位详情页 EdgeOne JS Challenge 纯协议链路
  1. GET 详情页 (无 cookie) -> challenge HTML
  2. 提取 script, Node 执行 -> EO-Bot-Js-Token
  3. 带 cookie 重放 -> 真实职位数据?

用法:
  py js_reverse_cache/e2e_detail.py [--url 职位详情URL] [--save-html]
"""
import argparse
import json
import re
import subprocess
import sys
import time
import os

from curl_cffi import requests as cr

CACHE = os.path.dirname(os.path.abspath(__file__))
DETAIL = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm"


def fetch_shell(url, session=None):
    s = session or cr
    r = s.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
    html = r.text
    is_challenge = "solveChallenge" in html and "EO-Bot-Js-Token" in html
    is_data = "__INITIAL_STATE__" in html or "positionDetail" in html or "职位描述" in html
    print(f"首跳: status={r.status_code} len={len(html)} challenge壳={is_challenge} 真实数据={is_data} url={r.url}")
    return html, is_challenge


def extract_script(html):
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        return None
    return m.group(1)


def run_node(script):
    p = subprocess.run(
        ["node", os.path.join(CACHE, "eo_solve.js"), script],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(p.stdout)
    except Exception:
        print("node stdout:", p.stdout[:300])
        print("node stderr:", p.stderr[:500])
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DETAIL)
    ap.add_argument("--repeat", type=int, default=1, help="重放次数(验证可重复)")
    ap.add_argument("--clean-session", action="store_true", help="每次用全新 Session 验证 challenge 可重复")
    args = ap.parse_args()

    # 用干净 session 验证 challenge 流程
    if args.clean_session:
        sess = cr.Session(impersonate="chrome", timeout=25)
        html, is_challenge = fetch_shell(args.url, sess)
    else:
        html, is_challenge = fetch_shell(args.url)
    if not is_challenge:
        print("首跳即拿到数据, 无需 challenge (可能 IP 已被放行)")
        return

    # 保存首次 challenge 样本
    ts = int(time.time())
    with open(os.path.join(CACHE, "html", f"eo_challenge_{ts}.html"), "w", encoding="utf-8") as f:
        f.write(html)

    script = extract_script(html)
    if not script:
        print("提取 script 失败")
        return
    with open(os.path.join(CACHE, "scripts", f"eo_challenge_live_{ts}.js"), "w", encoding="utf-8") as f:
        f.write(script)

    # 执行 challenge
    print("\n== 执行 challenge JS ==")
    result = run_node(os.path.join(CACHE, "scripts", f"eo_challenge_live_{ts}.js"))
    if not result or not result.get("ok") or not result.get("token"):
        print("challenge 执行失败:", result)
        return
    token = result["token"]
    print(f"token 长度: {len(token)}  前缀: {token[:20]}  耗时字段 timestamp={result.get('timestamp')}")

    # 带 cookie 重放
    for i in range(args.repeat):
        print(f"\n== 重放 #{i+1} (带 EO-Bot-Js-Token) ==")
        req = sess if args.clean_session else cr
        r = req.get(args.url, impersonate="chrome", timeout=25, allow_redirects=True,
                    cookies={"EO-Bot-Js-Token": token})
        body = r.text
        is_data = "__INITIAL_STATE__" in body or "positionDetail" in body or "职位描述" in body or "职位名称" in body
        is_challenge2 = "solveChallenge" in body
        print(f"  status={r.status_code} len={len(body)} 真实数据={is_data} 又遇challenge={is_challenge2}")
        if is_data:
            with open(os.path.join(CACHE, "html", f"detail_data_{i}.html"), "w", encoding="utf-8") as f:
                f.write(body)
            print("  ✅ 拿到真实职位数据! 已保存 detail_data_%d.html" % i)
        elif is_challenge2:
            print("  ⚠️ 仍是 challenge, token 未被接受")
            # 看响应是否给出新 challenge
            if "验证" in body and "Security" in body:
                print("  (EdgeOne 交互验证码页)")
        else:
            print("  (未知响应形态) len=%d" % len(body))
        if args.repeat > 1:
            time.sleep(2)


if __name__ == "__main__":
    main()
