# -*- coding: utf-8 -*-
"""
DP 分析: 真实 .htm 页面 SSR 数据提取 + 翻页交互穷举

关键问题: count=27 但 SSR 只有 20 条 (第1页), 剩余 7 条如何加载?
翻页交互穷举: 点击页码/下一页/滚动, 记录 URL 变化 + 新请求。
"""
import json
import os
import re
import time
from pathlib import Path

from DrissionPage import Chromium

COMPANY_URL = "https://www.zhaopin.com/companydetail/CZL1425835260.htm"
OUT_DIR = Path(os.environ.get("ZHAOPIN_CAPTURE_DIR", "output"))


def safe_wait(tab, count, timeout):
    try:
        res = tab.listen.wait(count, timeout=timeout, fit_count=False)
    except Exception:  # noqa: BLE001
        return []
    if res is False or not res:
        return []
    return res if isinstance(res, list) else [res]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    browser = Chromium()
    tab = browser.latest_tab
    tab.set.window.max()
    tab.get(COMPANY_URL)
    tab.wait.doc_loaded()
    tab.wait(3)

    # 1) 保存 SSR state
    html = tab.html
    m = re.search(r"__INITIAL_STATE__=(\{.*?\});?\s*</script>", html, re.DOTALL)
    if m:
        try:
            st = json.loads(m.group(1))
            with open(OUT_DIR / "initial_state_CZL1425835260_htm.json", "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
            op = st.get("onlinePositions") or []
            print("SSR: searchPositionsCount=", st.get("searchPositionsCount"),
                  "onlinePositionsPage=", st.get("onlinePositionsPage"),
                  "pageIndex=", st.get("pageIndex"), "onlinePositions=", len(op))
            # 找翻页元信息
            for k in ("onlinePositionsPage", "pageIndex", "totalPage", "totalCount"):
                print(f"  {k} = {st.get(k)}")
        except Exception as e:  # noqa: BLE001
            print("SSR 解析失败:", e)
    else:
        print("无 __INITIAL_STATE__")

    # 2) 页面当前 URL 与结构
    print("\n当前 URL:", tab.url)

    # 3) 翻页交互穷举: 监听 URL 变化 + 新请求
    tab.listen.start(["cgate.zhaopin.com", "searchPositionsCompany", "companyJobList",
                      "fe-api.zhaopin.com/c/i/company"], is_regex=False)
    print("\n--- 翻页交互穷举 ---")
    selectors = [
        ("css:.page-item:not(.active)", "页码项"),
        ("css:.ant-pagination-item", "ant页码"),
        ("css:.zp-page-item", "zp页码"),
        ("css:[class*=page] [class*=item]", "page-item子"),
        ("text:下一页", "下一页"),
        ("text:2", "文本2"),
        ("css:.next", "next类"),
        ("css:.zp-page-next", "zp下一页"),
    ]
    for sel, label in selectors:
        try:
            els = tab.eles(sel, timeout=1.5)
            if els:
                print(f"发现 [{label}] {sel}: {len(els)} 个")
                for el in els[:3]:
                    try:
                        t = (el.text or "").strip()[:20]
                        print(f"   -> text={t!r} tag={el.tag} cls={(el.attr('class') or '')[:40]}")
                    except Exception:  # noqa: BLE001
                        pass
                # 点击第一个
                try:
                    el = els[0]
                    before_url = tab.url
                    el.click()
                    tab.wait(2.5)
                    after_url = tab.url
                    if after_url != before_url:
                        print(f"   ★点击后 URL 变化: {before_url} -> {after_url}")
                    pkts = safe_wait(tab, 3, 3)
                    for p in pkts:
                        if p:
                            print(f"   ★请求: {p.method} {p.url[:130]}")
                    if not pkts and after_url == before_url:
                        print("   (无 URL 变化, 无新请求)")
                except Exception as e:  # noqa: BLE001
                    print("   click err:", e)
                break  # 只处理第一个命中的 selector 组
        except Exception:  # noqa: BLE001
            continue

    tab.listen.stop()
    print("\n完成")


if __name__ == "__main__":
    main()
