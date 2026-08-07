# -*- coding: utf-8 -*-
"""iv8 分步加载 widget_ele_eo.html: 先 HTML 后脚本"""
import json
import iv8
from pathlib import Path

D = Path(__file__).resolve().parent / "tasks" / "zhilian-tdc-iv8"
HTML = (D / "widget_ele_eo.html").read_text(encoding="utf-8")
DYJY = (D / "dy_jy.js").read_text(encoding="utf-8")
WIDGET = (D / "widget_ele_current.js").read_text(encoding="utf-8")

BASE = "https://captcha.eo.gtimg.com/static/template/widget_ele_eo.html"


def step(step_name, fn, ctx):
    print(f"\n=== {step_name} ===")
    try:
        r = fn(ctx)
        print("OK:", r)
    except Exception as e:
        print("ERR:", str(e)[:400])


with iv8.JSContext(environment={"window": {"name": "https://captcha.eo.qq.com|https://captcha.eo.gtimg.com/static"}}) as ctx:
    step("step0 window.name", lambda c: c.eval("window.name"), ctx)
    step("step1 load HTML only", lambda c: c.eval(f"""
        window.__iv8__.page.load({{ baseURL: "{BASE}", html: {json.dumps(HTML)}, resources: {{}} }});
        'html loaded'
    """), ctx)
    step("step2 body title", lambda c: c.eval("document.title"), ctx)
    step("step3 TCaptchaApiDomain", lambda c: c.eval("window.TCaptchaApiDomain || 'N/A'"), ctx)
    step("step4 jQuery after dy-jy", lambda c: c.eval(f"""
        new Function({json.dumps(DYJY)})();
        (typeof window.jQuery) + ' ' + (typeof window.$)
    """), ctx)
    step("step5 widget-ele eval", lambda c: c.eval(f"""
        new Function({json.dumps(WIDGET)})();
        (typeof window.TDC) + ' | keys: ' + (window.TDC ? Object.keys(window.TDC).join(',') : '')
    """), ctx)
