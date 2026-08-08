# -*- coding: utf-8 -*-
"""
DP 诊断: 公司详情页所有 cgate 请求全景 + 页面状态

先看真实接口路径是什么, 再谈匹配。监听 cgate.zhaopin.com 下所有请求。
"""
import json

from DrissionPage import Chromium

COMPANY_URL = "https://www.zhaopin.com/companydetail/jobs-CZL1425835260/"


def main():
    browser = Chromium()
    tab = browser.latest_tab
    tab.set.window.max()
    print(f"打开: {COMPANY_URL}")

    # 监听 cgate 域名所有请求 (模糊)
    tab.listen.start("cgate.zhaopin.com", is_regex=False, res_type=True)
    tab.get(COMPANY_URL)
    tab.wait.doc_loaded()
    tab.wait(3)

    print("\n--- 捕获的数据包 (cgate) ---")
    try:
        pkts = tab.listen.wait(20, timeout=15, fit_count=False)
        if not pkts:
            print("未捕获到任何 cgate 请求")
        for p in pkts:
            if not p:
                continue
            try:
                print(f"{p.method} {p.url[:140]}")
            except Exception as e:  # noqa: BLE001
                print("pkt 异常:", e)
    except Exception as e:  # noqa: BLE001
        print("wait 异常:", e)

    # 页面状态
    print("\n--- 页面状态 ---")
    try:
        print("url   :", tab.url)
        print("title :", tab.title)
        # 找"在招职位"相关文本/数字
        for sel in ("text:在招职位", "text:在招岗位", "text:全部职位", "text:招聘职位"):
            try:
                el = tab.ele(sel, timeout=1)
                if el:
                    print("元素:", sel, "=>", (el.text or "")[:60])
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        print("页面状态读取失败:", e)

    tab.listen.stop()
    print("\n完成")


if __name__ == "__main__":
    main()
