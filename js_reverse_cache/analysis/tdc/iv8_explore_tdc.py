# -*- coding: utf-8 -*-
"""
iv8 探索: TDC API 方法 + 挑战全局注入 + setData 序列
"""
import iv8
from pathlib import Path

TDC = Path("js_reverse_cache/tasks/zhilian-tdc-iv8/tdc_dynamic.js").read_text(encoding="utf-8")

with iv8.JSContext() as ctx:
    ctx.eval(TDC)
    print("typeof window.TDC:", ctx.eval("typeof window.TDC"))
    print("TDC methods:", ctx.eval("Object.keys(window.TDC || {})"))
    print("TDC proto:", ctx.eval("Object.getOwnPropertyNames(Object.getPrototypeOf(window.TDC) || {})"))

    print("\n=== 挑战全局注入 ===")
    # 模拟 iframe 环境 (TDC 工作流要求)
    inject = r"""
    TCaptchaSid = "s1OUjTauC-ULrAhEmt8o";  // 该轮 sess 前缀(示意)
    TCaptchaReferrer = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm";
    TCaptchaIframeClientPos = {x: 128, y: 320, w: 320, h: 160};
    window.name = "TCD0ZZFBJNH8MKS7RBVN";
    """
    try:
        ctx.eval(inject)
        print("全局注入 OK")
    except Exception as e:
        print("注入异常:", str(e)[:300])

    print("\n=== setData 序列 ===")
    try:
        ctx.eval("""
        TDC.setData({isNewEntry: 1});
        """)
        print("setData(isNewEntry) OK")
    except Exception as e:
        print("setData 异常:", str(e)[:300])

    print("\n=== getInfo ===")
    try:
        r = ctx.eval("(TDC.getInfo() || {}).info || ''")
        print("info len:", len(r) if isinstance(r, str) else r)
        print("info[:120]:", str(r)[:120])
    except Exception as e:
        print("getInfo 异常:", str(e)[:300])

    print("\n=== getData(true) ===")
    try:
        r = ctx.eval("TDC.getData(true) || ''")
        print("getData len:", len(r) if isinstance(r, str) else r)
        print("getData[:200]:", str(r)[:200])
    except Exception as e:
        print("getData 异常:", str(e)[:300])
