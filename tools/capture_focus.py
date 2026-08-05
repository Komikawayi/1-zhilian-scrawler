# -*- coding: utf-8 -*-
"""
聚焦取证: 搜索接口响应体 + cookie/localStorage + JS bundle 源码

用法:
    python tools/capture_focus.py
"""
import asyncio
import json
import os
import re
import time

from cloakbrowser import browser as B

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "network")
SCRIPT_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "scripts")
BODY_DIR = os.path.join(NET_DIR, "bodies")
os.makedirs(SCRIPT_DIR, exist_ok=True)
os.makedirs(BODY_DIR, exist_ok=True)

TARGET = "https://sou.zhaopin.com/?jl=530&kw=python&p=1"


async def main() -> None:
    ctx = await B.launch_context_async(headless=False, locale="zh-CN", timezone="Asia/Shanghai")
    page = await ctx.new_page()

    search_bodies = {}     # url -> text
    js_bundles = {}        # url -> text
    other_js = []

    async def on_response(resp) -> None:
        url = resp.url
        try:
            ct = resp.headers.get("content-type", "")
            if "search/base/data" in url:
                search_bodies[url] = await resp.text()
                print(f"  [SEARCH RESP] {resp.status} len={len(search_bodies[url])} ct={ct}")
            elif ".js" in url.split("?")[0] and "zhaopin.com" in url:
                txt = await resp.text()
                js_bundles[url] = txt
                print(f"  [JS] {len(txt):>8} {url[:120]}")
            elif "json" in ct and "zhaopin.com" in url:
                txt = await resp.text()
                other_js.append((url, txt))
        except Exception as e:
            print(f"  [ERR] {url[:80]} {type(e).__name__} {str(e)[:80]}")

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    try:
        await page.goto(TARGET, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)
    except Exception as e:
        print("NAV-ERR:", type(e).__name__, str(e)[:200])

    print("TITLE:", await page.title())
    print("FINAL URL:", page.url)

    # cookie + storage
    cookies = await ctx.cookies()
    print(f"\n=== COOKIES ({len(cookies)}) ===")
    for c in cookies:
        print(f"  {c['name']}={c['value'][:120]}  domain={c['domain']}")
    for store_name in ("localStorage", "sessionStorage"):
        try:
            vals = await page.evaluate(f"(function(){{ var o={{}}; for(var i=0;i<{store_name}.length;i++){{ var k={store_name}.key(i); o[k]={store_name}.getItem(k); }} return o; }})()")
            print(f"\n=== {store_name} ({len(vals)}) ===")
            for k, v in vals.items():
                print(f"  {k} = {str(v)[:200]}")
        except Exception as e:
            print(store_name, "ERR", str(e)[:100])

    # 保存 search 响应
    for url, text in search_bodies.items():
        fname = "search_base_data_response.txt"
        with open(os.path.join(BODY_DIR, fname), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n=== search/base/data RESPONSE ({len(text)} chars) preview ===")
        print(text[:1500])

    # 保存 JS bundle
    for url, text in js_bundles.items():
        fname = url.split("/")[-1].split("?")[0]
        with open(os.path.join(SCRIPT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(text)

    # 保存 cookie/localStorage 快照
    snap = {
        "cookies": cookies,
        "final_url": page.url,
    }
    with open(os.path.join(NET_DIR, "state_snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)

    print(f"\nJS bundles saved: {len(js_bundles)}")
    await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
