# -*- coding: utf-8 -*-
"""
TCaptcha 同轮证据采集 - 阶段 1: 直接调 cap_union_prehandle
智联 EdgeOne appid=25200697 (静态), domain=captcha.eo.gtimg.com
尝试不同 showtype/clientype 组合, 保存有效 prehandle 响应
"""
import json
import time
import base64
import sys
from pathlib import Path

from curl_cffi import requests as creq

OUT = Path(__file__).resolve().parent / "js_reverse_cache" / "tasks" / "zhilian-tdc-iv8"
OUT.mkdir(parents=True, exist_ok=True)

APPID = "25200697"
SID = "1343190090"
DEVICE_ID = "1018152636"
UUID = "4117903521339852012"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
UA_B64 = base64.b64encode(UA.encode()).decode()

HEADERS = {
    "user-agent": UA,
    "referer": "https://www.zhaopin.com/",
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="146", "Google Chrome";v="146", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "origin": "https://www.zhaopin.com",
}


def build_params(showtype: str, clientype: str) -> str:
    """复刻 TEOCaptchaWidget.js 的 s_type_suffix 构造"""
    t = []
    t.append(f"aid={APPID}")
    t.append("protocol=https")
    t.append("accver=1")
    t.append(f"showtype={showtype}")
    t.append("ua=" + __import__("urllib.parse").parse.quote(UA_B64, safe=""))
    t.append("noheader=0")
    t.append("fb=1")
    t.append("aged=0")
    t.append("enableAged=0")
    t.append("enableDarkMode=0")
    t.append(f"deviceID={DEVICE_ID}")
    t.append("passthrough=")
    t.append(f"uuid={UUID}")
    t.append(f"sid={SID}")
    t.append("uid=")
    t.append("grayscale=1")
    t.append("dyeid=0")
    t.append(f"clientype={clientype}")
    return "?" + "&".join(t)


def probe(showtype: str, clientype: str):
    url = "https://captcha.eo.qq.com/cap_union_prehandle" + build_params(showtype, clientype)
    print(f"\n=== prehandle {showtype}/cp{clientype} ===")
    try:
        r = creq.get(url, headers=HEADERS, impersonate="chrome", timeout=20)
        print("HTTP", r.status_code, "| len", len(r.content))
        try:
            raw = r.text.strip()
            if raw.startswith("(") and raw.endswith(")"):
                raw = raw[1:-1]
            d = json.loads(raw)
            print("keys:", list(d.keys())[:12])
            # 关键字段
            state = d.get("state")
            print("state:", state, "| error:", d.get("error"))
            for k in ("sess", "sid", "pow_cfg", "dyn_show_info", "tdc_path", "comm_captcha_cfg", "cap_cd", "appid"):
                v = d.get(k)
                if isinstance(v, dict):
                    print(f"  {k}: keys={list(v.keys())[:8]}")
                elif v is not None:
                    print(f"  {k}: {str(v)[:100]}")
            fn = OUT / f"prehandle_{showtype}_cp{clientype}.json"
            fn.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            print("saved:", fn.name)
            return d
        except Exception as e:
            print("非 JSON:", str(e)[:200])
            print(r.text[:300])
    except Exception as e:
        print("请求异常:", str(e)[:200])
    return None


if __name__ == "__main__":
    combos = [
        ("popup", "2"),
        ("embed", "2"),
        ("point", "2"),
        ("popup", "1"),
        ("embed", "1"),
    ]
    ok = None
    for st, cp in combos:
        d = probe(st, cp)
        if d and d.get("state") == 1:
            ok = (st, cp, d)
            break
        time.sleep(1)
    if not ok:
        print("\n全部组合未返回 state==1")
    else:
        print(f"\n✅ 有效组合: showtype={ok[0]} clientype={ok[1]}")
