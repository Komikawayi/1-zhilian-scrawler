# -*- coding: utf-8 -*-
"""
保存 SSR HTML 并翻页取证: 分析职位数据来源 + kw 编码逻辑

用法:
    python tools/capture_html.py [--kw python] [--jl 530]
"""
import argparse
import asyncio
import json
import os
import re

from cloakbrowser import browser as B

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "network")
HTML_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "html")
SCRIPT_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "scripts")
os.makedirs(HTML_DIR, exist_ok=True)
os.makedirs(SCRIPT_DIR, exist_ok=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="python")
    ap.add_argument("--jl", default="530")
    args = ap.parse_args()

    ctx = await B.launch_context_async(headless=False, locale="zh-CN", timezone="Asia/Shanghai")
    page = await ctx.new_page()

    js_saved = {}

    async def on_response(resp) -> None:
        url = resp.url
        try:
            ct = resp.headers.get("content-type", "")
            if ".js" in url.split("?")[0] or "javascript" in ct:
                if "zhaopin.com" in url:
                    txt = await resp.text()
                    if len(txt) < 5_000_000:
                        js_saved[url] = txt
                        print(f"  [JS] {len(txt):>8} {url.split('/')[-1][:70]}")
        except Exception:
            pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    urls = [
        ("p1", f"https://sou.zhaopin.com/?jl={args.jl}&kw={args.kw}&p=1"),
        ("p2", f"https://sou.zhaopin.com/?jl={args.jl}&kw={args.kw}&p=2"),
    ]
    for name, url in urls:
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(9000)
        except Exception as e:
            print(f"[{name}] NAV-ERR: {e}")
        html = await page.content()
        final_url = page.url
        with open(os.path.join(HTML_DIR, f"{name}_{final_url.split('/')[-1]}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[{name}] final_url={final_url} html_len={len(html)}")
        # 分析 HTML
        for pat in ("__INITIAL_STATE__", "__NUXT__", "__NEXT_DATA__", "window.__", "initialState", "FSSBB", "__INITIAL_", "jobList", "positionList"):
            if pat in html:
                print(f"    含: {pat}")

    # 保存 JS
    for url, txt in js_saved.items():
        fname = url.split("?")[0].split("/")[-1]
        if not fname.endswith(".js"):
            fname = re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("/")[-1][:60]) + ".js"
        with open(os.path.join(SCRIPT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(txt)
    print(f"\nJS saved: {len(js_saved)}")
    await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
