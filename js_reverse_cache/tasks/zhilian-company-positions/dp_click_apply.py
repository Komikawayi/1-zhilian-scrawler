# -*- coding: utf-8 -*-
"""
DP 分析: 从 companydetail/CZL1425835260.htm 页面点击"在招岗位"抓 cgate 请求

路径: 打开 .htm 公司页 -> 点击"在招岗位" tab -> 抓取 cgate searchPositionsCompany
-> 再尝试底部翻页, 观察翻页是否有新请求 (用户观察到翻页无变化)。
"""
import json

from DrissionPage import Chromium

COMPANY_URL = "https://www.zhaopin.com/companydetail/CZL1425835260.htm"


def safe_wait(tab, count, timeout):
    """安全封装 listen.wait: 避免返回 False 时迭代报错。"""
    try:
        res = tab.listen.wait(count, timeout=timeout, fit_count=False)
    except Exception as e:  # noqa: BLE001
        print("  wait 异常:", e)
        return []
    if res is False or not res:
        return []
    if isinstance(res, list):
        return res
    return [res]


def dump_full(pkt, tag):
    print(f"\n{'='*75}")
    print(f"[{tag}] {pkt.method} {pkt.url}")
    print(f"{'='*75}")
    r = pkt.request
    print("params   :", json.dumps(r.params, ensure_ascii=False))
    print("postData :", json.dumps(r.postData, ensure_ascii=False))
    ck = [f"{c.get('name')}" for c in (r.cookies or [])]
    print("cookie 键:", ck)
    hdrs = {k: v for k, v in r.headers.items() if k.lower() not in ("cookie",)}
    # 精简: 只打印关键头
    key_h = {k: v for k, v in hdrs.items() if k.lower() in (
        "content-type", "accept", "origin", "referer", "x-zp-platform", "x-zp-business-system",
        "x-zp-page-code", "user-agent", "content-length")}
    print("headers  :", json.dumps(key_h, ensure_ascii=False))
    try:
        b = pkt.response.body
        if isinstance(b, dict):
            d = b.get("data") or {}
            lst = d.get("list") or []
            print("resp: code=", b.get("statusCode"), "count=", d.get("count"),
                  "list=", len(lst), "isEndPage=", d.get("isEndPage"),
                  "pageIndex=", d.get("pageIndex"), "pageSize=", d.get("pageSize"))
            if lst:
                print("  first:", lst[0].get("number"), "| companyId=", lst[0].get("companyId"),
                      "| companyNumber=", lst[0].get("companyNumber"), "| name=", lst[0].get("companyName"))
        elif isinstance(b, str):
            print("resp.text:", b[:200])
    except Exception as e:  # noqa: BLE001
        print("resp err:", e)


def main():
    browser = Chromium()
    tab = browser.latest_tab
    tab.set.window.max()
    print(f"打开: {COMPANY_URL}")
    tab.get(COMPANY_URL)
    tab.wait.doc_loaded()
    tab.wait(3)

    # 监听 cgate 相关接口 (覆盖 searchPositionsCompany / companyJobList 等)
    tab.listen.start(["cgate.zhaopin.com", "searchPositionsCompany", "companyJobList"], is_regex=False)

    # 点击"在招岗位" tab (可能是: 在招岗位/在招职位/全部职位)
    print("\n--- 点击'在招岗位' tab ---")
    clicked = None
    for sel in ("text:在招岗位", "text:在招职位", "text:全部职位", "text:招聘岗位"):
        try:
            el = tab.ele(sel, timeout=1.5)
            if el:
                clicked = sel
                print("点击:", sel, "=>", (el.text or "")[:30])
                el.click()
                tab.wait(3)
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        print("未找到 tab 元素, 直接等待页面自动加载")

    # 抓点击后/页面加载产生的 cgate 请求
    print("\n--- 捕获的 cgate 请求 ---")
    pkts = safe_wait(tab, 10, 8)
    if not pkts:
        print("无 cgate 请求 (页面可能 SSR 直出)")
    for p in pkts:
        try:
            if p:
                dump_full(p, "cgate")
        except Exception as e:  # noqa: BLE001
            print("pkt err:", e)

    # 底部翻页观察
    print("\n--- 底部翻页观察 ---")
    # 先看页面有没有分页/加载更多
    for sel in ("css:.pagination", "css:.zp-page", "css:[class*=pagination]", "css:[class*=page-btn]", "text:下一页", "text:加载更多"):
        try:
            el = tab.ele(sel, timeout=1)
            if el:
                print("发现元素:", sel, "=>", (el.text or "")[:60])
        except Exception:  # noqa: BLE001
            continue
    # 滚动到底部
    try:
        tab.scroll.to_bottom()
        tab.wait(3)
    except Exception:  # noqa: BLE001
        pass
    pkts = safe_wait(tab, 5, 5)
    if pkts:
        for p in pkts:
            if p:
                dump_full(p, "滚动后")
    else:
        print("滚动到底无新 cgate 请求")

    tab.listen.stop()
    print("\n完成. 浏览器保持打开供人工检查.")


if __name__ == "__main__":
    main()
