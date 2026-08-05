# -*- coding: utf-8 -*-
"""
智联招聘搜索接口基线捕获工具 (CloakBrowser + CDP)

用法:
    python tools/capture_baseline.py

功能:
    1. 启动本地 CloakBrowser (有头模式, zh-CN 时区)
    2. CDP Network 捕获: requestWillBeSent (完整 header/postData/initiator)
    3. 捕获 zhaopin 相关接口的响应体
    4. 对多组 关键词/翻页 导航, 对比线路上的 kw 编码与动态参数
    5. 所有样本写入 js_reverse_cache/
"""
import asyncio
import json
import os
import sys
import uuid

from cloakbrowser import browser as B

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET_DIR = os.path.join(BASE_DIR, "js_reverse_cache", "network")
BODY_DIR = os.path.join(NET_DIR, "bodies")
os.makedirs(BODY_DIR, exist_ok=True)

SESSIONS = [
    {"name": "py_p1", "url": "https://sou.zhaopin.com/?jl=530&kw=python&p=1"},
    {"name": "java_p1", "url": "https://sou.zhaopin.com/?jl=530&kw=java&p=1"},
    {"name": "py_p2", "url": "https://sou.zhaopin.com/?jl=530&kw=python&p=2"},
]

TARGET_HOSTS = ("zhaopin.com",)
# 只保存接口/脚本类请求, 排除静态图片字体等
SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".svg", ".webp", ".css")


def is_interesting(url: str) -> bool:
    return any(h in url for h in TARGET_HOSTS) and not any(url.endswith(e) for e in SKIP_EXT)


async def main() -> None:
    ctx = await B.launch_context_async(headless=False, locale="zh-CN", timezone="Asia/Shanghai")
    page = await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)

    requests = []          # CDP requestWillBeSent
    bodies = {}            # url -> response body text (truncated)
    events = {"req": requests}

    def on_request_will_be_sent(params: dict) -> None:
        req = params.get("request", {})
        url = req.get("url", "")
        if not is_interesting(url):
            return
        requests.append({
            "requestId": params.get("requestId"),
            "session": events.get("cur_session"),
            "url": url,
            "method": req.get("method"),
            "headers": req.get("headers", {}),
            "postData": req.get("postData"),
            "initiator": params.get("initiator"),
            "timestamp": params.get("timestamp"),
        })

    async def on_response(resp) -> None:
        url = resp.url
        if not is_interesting(url):
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct or "javascript" in ct or "x-www-form-urlencoded" in ct or resp.request.method in ("POST", "PUT"):
                text = await resp.text()
                if len(text) < 2_000_000:
                    bodies[url] = text
        except Exception:
            pass

    await cdp.send("Network.enable")
    cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    for sess in SESSIONS:
        events["cur_session"] = sess["name"]
        try:
            await page.goto(sess["url"], timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(9000)
        except Exception as e:
            print(f"[{sess['name']}] NAV-ERR: {type(e).__name__} {str(e)[:200]}")
        print(f"[{sess['name']}] title={await page.title()}")
        print(f"[{sess['name']}] url={page.url}")

    # 保存样本
    meta = {
        "captured_at": __import__("time").time(),
        "browser": "cloakbrowser-0.5.3-chromium-146",
        "request_count": len(requests),
        "body_count": len(bodies),
        "requests": requests,
    }
    with open(os.path.join(NET_DIR, "baseline_requests.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    for url, text in bodies.items():
        fname = url.split("?")[0].replace("https://", "").replace("/", "_").replace(".", "_")[:120]
        with open(os.path.join(BODY_DIR, fname + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)

    # 打印搜索相关请求
    print("\n=== SEARCH/API 请求样本 ===")
    for r in requests:
        if "search" in r["url"] or "sou" in r["url"]:
            print(f"\n[{r['session']}] {r['method']} {r['url'][:220]}")
            hdr = r["headers"]
            for k in ("x-zp-page-request-id", "x-zp-client-id", "x-zp-request-id", "x-zp-client-version", "x-zp-device-id", "x-zp-sign", "x-zp-traceid", "referer", "user-agent"):
                if k in hdr:
                    print(f"    {k}: {hdr[k][:120]}")
            if r.get("postData"):
                print(f"    postData: {r['postData'][:300]}")
            ini = r.get("initiator") or {}
            print(f"    initiator: type={ini.get('type')} stack_top={str(ini.get('stack', {}).get('callFrames', [{}])[0].get('functionName', '') if ini.get('stack', {}).get('callFrames') else '')[:80]} url={str(ini.get('stack', {}).get('callFrames', [{}])[0].get('url', '') if ini.get('stack', {}).get('callFrames') else '')[:100]}")
            if r["url"] in bodies:
                print(f"    response_len={len(bodies[r['url']])} preview={bodies[r['url']][:200]}")

    print(f"\n已保存: {NET_DIR}/baseline_requests.json  +  bodies/ 共 {len(bodies)} 个响应体")
    await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
