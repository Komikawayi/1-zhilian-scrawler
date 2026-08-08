# -*- coding: utf-8 -*-
"""
DP 分析: 智联公司详情页 jobs 在招职位翻页真实请求抓包

用途: 打开真实公司页, 监听 cgate searchPositionsCompany 请求,
对比首屏 vs 翻页的真实参数差异, 确认纯协议所需字段。

目标公司: CZL1425835260 (用户在浏览器看到的, 27 个在招岗位)
"""
import json
import sys

from DrissionPage import Chromium

COMPANY_URL = "https://www.zhaopin.com/companydetail/jobs-CZL1425835260/"
TARGET = "searchPositionsCompany"  # 监听特征


def dump_request(pkt, tag):
    print(f"\n{'='*70}")
    print(f"[{tag}] {pkt.method} {pkt.url}")
    print(f"{'='*70}")
    r = pkt.request
    print("params   :", json.dumps(r.params, ensure_ascii=False, indent=1) if r.params else "(无)")
    print("postData :", json.dumps(r.postData, ensure_ascii=False, indent=1) if r.postData else "(无)")
    # 关键 headers (过滤常见无用的)
    keep = ["content-type", "x-zp-", "origin", "referer", "user-agent", "cookie", "accept"]
    hdrs = {k: v for k, v in r.headers.items() if any(kk in k.lower() for kk in keep)}
    # cookie 太长, 只打 key
    cookies = r.cookies
    ck_keys = [c.get("name") for c in cookies] if cookies else []
    print("headers  :", json.dumps({k: (v[:200] + "..." if len(v) > 200 else v) for k, v in hdrs.items()}, ensure_ascii=False))
    print("cookies  :", ck_keys)
    # 响应摘要
    try:
        body = pkt.response.body
        if isinstance(body, dict):
            data = body.get("data") or {}
            print("resp.code:", body.get("statusCode"), "| count:", data.get("count"),
                  "| list:", len(data.get("list", [])) if isinstance(data.get("list"), list) else data.get("list"),
                  "| isEndPage:", data.get("isEndPage"))
            if isinstance(data.get("list"), list) and data["list"]:
                it = data["list"][0]
                print("  first:", json.dumps({k: it.get(k) for k in ("number", "companyName", "salary60", "workCity")}, ensure_ascii=False))
                print("  first.keys 共", len(it.keys()), "个")
        else:
            print("resp.body (非 dict):", str(body)[:200])
    except Exception as e:  # noqa: BLE001
        print("resp 读取失败:", e)


def main():
    browser = Chromium()  # 默认 9222, 无则启动; 有则接管(保留登录态)
    tab = browser.latest_tab
    tab.set.window.max()
    print(f"打开: {COMPANY_URL}")

    # 先启动监听, 再访问 (listen.start 之前的数据包拿不到)
    tab.listen.start(TARGET, is_regex=False)
    tab.get(COMPANY_URL)
    tab.wait.doc_loaded()

    # 等首屏请求
    try:
        pkt = tab.listen.wait(timeout=20)
        dump_request(pkt, "首屏")
    except Exception as e:  # noqa: BLE001
        print("首屏未捕获到", TARGET, ":", e)
        # 兜底: dump 已监听队列
        try:
            for p in tab.listen.wait(10, timeout=5, fit_count=False):
                if p:
                    dump_request(p, "兜底")
        except Exception:  # noqa: BLE001
            pass

    # 尝试找"下一页"并点击 (公司在招职位分页控件)
    print("\n--- 尝试翻页 ---")
    for _ in range(3):
        try:
            # 常见翻页: rel=next / class 含 next / page
            nxt = None
            for sel in ("@rel=next", "text:下一页", "css:.next", "css:[class*=next]"):
                try:
                    nxt = tab.ele(sel, timeout=1.5)
                    if nxt:
                        print("找到翻页元素:", sel)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not nxt:
                print("未找到翻页元素, 停止")
                break
            nxt.click()
            tab.wait(2)
            pkts = tab.listen.wait(3, timeout=8, fit_count=False)
            for p in pkts:
                if p:
                    dump_request(p, "翻页")
        except Exception as e:  # noqa: BLE001
            print("翻页失败:", e)
            break

    tab.listen.stop()
    print("\n完成. 浏览器保持打开, 可人工检查.")


if __name__ == "__main__":
    main()
