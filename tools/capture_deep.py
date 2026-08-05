# -*- coding: utf-8 -*-
"""
深度侦察: 触发职位列表加载, 抓全量 JS chunk + 定位职位列表接口

用法:
    python tools/capture_deep.py [--kw python] [--jl 530] [--pages 2]
"""
import argparse
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

SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".svg", ".webp", ".css", ".map")


def is_target(url: str) -> bool:
    return "zhaopin.com" in url and not any(url.endswith(e) for e in SKIP_EXT)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="python")
    ap.add_argument("--jl", default="530")
    ap.add_argument("--pages", type=int, default=2)
    args = ap.parse_args()

    ctx = await B.launch_context_async(headless=False, locale="zh-CN", timezone="Asia/Shanghai")
    page = await ctx.new_page()

    requests = []
    bodies = {}
    js_saved = {}

    async def on_response(resp) -> None:
        url = resp.url
        if not is_target(url):
            return
        try:
            ct = resp.headers.get("content-type", "")
            if ".js" in url.split("?")[0] or "javascript" in ct:
                txt = await resp.text()
                if len(txt) < 5_000_000:
                    js_saved[url] = txt
                    print(f"  [JS] {len(txt):>8} {url.split('/')[-1][:70]}")
            elif "json" in ct or resp.request.method == "POST":
                txt = await resp.text()
                if len(txt) < 5_000_000:
                    bodies[url] = txt
        except Exception:
            pass

    async def on_request(req) -> None:
        url = req.url
        if is_target(url):
            requests.append({"method": req.method, "url": url, "headers": req.headers})

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    page.on("request", lambda r: asyncio.ensure_future(on_request(r)))

    start_url = f"https://sou.zhaopin.com/?jl={args.jl}&kw={args.kw}&p=1"
    try:
        await page.goto(start_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(12000)
    except Exception as e:
        print("NAV-ERR:", type(e).__name__, str(e)[:200])

    # 滚动触发懒加载
    for i in range(6):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight/6)")
            await page.wait_for_timeout(2500)
        except Exception:
            break

    # 页面状态检查
    try:
        page_html = await page.content()
        title = await page.title()
        print("\nTITLE:", title)
        print("URL:", page.url)
        # 是否验证码/拦截
        for marker in ("验证", "captcha", "拦截", "安全", "FSSBB", "风险"):
            if marker in page_html:
                print(f"  页面含标记: {marker}")
        # 职位元素?
        jobs = await page.evaluate(
            "() => document.querySelectorAll('[class*=job], [class*=Job], [class*=position], [class*=Position]').length"
        )
        print("  job 相关 DOM 节点数:", jobs)
        # 尝试读职位文本
        txt = await page.evaluate("() => document.body.innerText.slice(0, 600)")
        print("  页面文本预览:", txt.replace("\\n", " ")[:500])
    except Exception as e:
        print("PAGE-STATE-ERR:", str(e)[:150])

    # 输出所有 API 请求
    print("\n=== 请求清单 (method url) ===")
    seen = {}
    for r in requests:
        u = r["url"].split("?")[0]
        seen[(r["method"], u)] = seen.get((r["method"], u), 0) + 1
    for (m, u), c in sorted(seen.items()):
        if any(d in u for d in ("fe-api", "cgate", "api", "sou", "widget")):
            print(f"  {m:6} x{c:<2} {u}")

    # 保存 JS
    for url, txt in js_saved.items():
        fname = url.split("?")[0].split("/")[-1]
        if not fname.endswith(".js"):
            fname = re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("/")[-1][:60]) + ".js"
        with open(os.path.join(SCRIPT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(txt)

    # 保存请求+body 样本
    with open(os.path.join(NET_DIR, "deep_requests.json"), "w", encoding="utf-8") as f:
        json.dump({"requests": requests}, f, ensure_ascii=False, indent=1)
    for url, txt in bodies.items():
        fname = url.split("?")[0].replace("https://", "").replace("/", "_").replace(".", "_")[:120]
        with open(os.path.join(BODY_DIR, fname + ".json"), "w", encoding="utf-8") as f:
            f.write(txt)
    print(f"\nJS total: {len(js_saved)}, bodies: {len(bodies)}")
    await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
