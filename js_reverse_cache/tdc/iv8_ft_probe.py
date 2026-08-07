# -*- coding: utf-8 -*-
"""
iv8: patch widget_ele.js 暴露 webpack require, 直接调用 module 48 (ft 生产者)
"""
import json
import iv8
from pathlib import Path

D = Path(__file__).resolve().parent / "tasks" / "zhilian-tdc-iv8"
DYJY = (D / "dy_jy.js").read_text(encoding="utf-8")
WIDGET = (D / "widget_ele_current.js").read_text(encoding="utf-8")

# patch: 在 webpack 入口执行前暴露 require 函数 r
MARK = "r(r.s=133)"
REPL = "window.__r=r;try{r(r.s=133)}catch(__e){window.__entryErr=String(__e&&__e.message||__e)}"
assert MARK in WIDGET, "入口标记未找到"
WIDGET_P = WIDGET.replace(MARK, REPL, 1)
print("patch OK")

with iv8.JSContext() as ctx:
    ctx.eval(f"new Function({json.dumps(DYJY)})();")
    print("jQuery:", ctx.eval("typeof window.jQuery"))
    try:
        ctx.eval(f"new Function({json.dumps(WIDGET_P)})();")
        print("widget_ele eval OK")
    except Exception as e:
        print("widget_ele ERR:", str(e)[:400])
    print("entryErr:", ctx.eval("window.__entryErr || 'none'"))
    print("__r:", ctx.eval("typeof window.__r"))

    # 调用 module 48 (ft 生产者)
    try:
        ft = ctx.eval("(function(){ var m = window.__r(48); var p = m && (m['default']||m); return (typeof p === 'function') ? p() : p; })()")
        print("\n=== ft 生产者输出 ===")
        print("type:", type(ft).__name__)
        print("ft:", str(ft)[:300] if ft else repr(ft))
    except Exception as e:
        print("module48 ERR:", str(e)[:400])
