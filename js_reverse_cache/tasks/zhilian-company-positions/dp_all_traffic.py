# -*- coding: utf-8 -*-
"""
DP 全量抓包: companydetail .htm 页面所有网络请求

目标: 确认职位列表/翻页真实走的接口域名与路径。
打开页面 -> 记录全部请求 -> 点击在招职位 -> 记录 -> 翻页 -> 记录。
"""
import json

from DrissionPage import Chromium

COMPANY_URL = "https://www.zhaopin.com/companydetail/CZL1425835260.htm"


def safe_wait(tab, count, timeout):
    try:
        res = tab.listen.wait(count, timeout=timeout, fit_count=False)
    except Exception:  # noqa: BLE001
        return []
    if res is False or not res:
        return []
    return res if isinstance(res, list) else [res]


def dump(pkt, tag):
    try:
        u = pkt.url
        m = pkt.method
        is_api = ("cgate" in u or "positionbusiness" in u or "searchrecommend" in u
                  or "companyJobList" in u or "apiDomain" in u or "fe-api" in u)
        mark = "★API" if is_api else "   "
        print(f"{mark}[{tag}] {m} {u[:150]}")
        if is_api:
            r = pkt.request
            print(f"        params={json.dumps(r.params, ensure_ascii=False)[:200]}")
            print(f"        postData={json.dumps(r.postData, ensure_ascii=False)[:200]}")
            try:
                b = pkt.response.body
                if isinstance(b, dict):
                    d = b.get("data") or {}
                    print(f"        resp code={b.get('statusCode')} count={d.get('count')} list={len(d.get('list') or []) if isinstance(d.get('list'), list) else d.get('list')}")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        print(f"pkt err {e}")


def main():
    browser = Chromium()
    tab = browser.latest_tab
    tab.set.window.max()

    # 全部请求监听
    tab.listen.start(True, is_regex=False)  # True = 获取所有
    print(f"打开: {COMPANY_URL}")
    tab.get(COMPANY_URL)
    tab.wait.doc_loaded()
    tab.wait(3)

    pkts = safe_wait(tab, 200, 5)
    print(f"\n=== 页面加载请求 ({len(pkts)}) ===")
    for p in pkts:
        if p:
            dump(p, "加载")

    # 点击"在招职位"
    print("\n=== 点击'在招职位' ===")
    try:
        el = tab.ele("text:在招职位", timeout=2)
        if el:
            el.click()
            tab.wait(3)
    except Exception:  # noqa: BLE001
        pass
    pkts = safe_wait(tab, 50, 4)
    print(f"点击后请求 ({len(pkts)}):")
    for p in pkts:
        if p:
            dump(p, "点击")

    # 底部翻页
    print("\n=== 底部翻页 ===")
    try:
        tab.scroll.to_bottom()
        tab.wait(2)
    except Exception:  # noqa: BLE001
        pass
    for sel in ("text:下一页", "css:.pagination", "css:[class*=page-btn]", "css:[class*=PageBtn]", "css:.page-item:last-child"):
        try:
            el = tab.ele(sel, timeout=1)
            if el and el.offset_parent is not None:
                print("翻页元素:", sel, "=>", (el.text or "")[:40])
                el.click()
                tab.wait(3)
                pkts = safe_wait(tab, 50, 4)
                for p in pkts:
                    if p:
                        dump(p, "翻页")
                break
        except Exception:  # noqa: BLE001
            continue

    tab.listen.stop()
    print("\n完成")


if __name__ == "__main__":
    main()
