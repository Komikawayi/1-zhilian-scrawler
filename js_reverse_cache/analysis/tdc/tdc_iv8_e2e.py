# -*- coding: utf-8 -*-
"""
智联 EdgeOne TCaptcha TDC — iv8 端到端落地 (研究脚本)

链路:
  prehandle (captcha.eo.qq.com) -> sess/pow_cfg/tdc_path
  -> 下载该轮动态 tdc.js
  -> iv8 执行 tdc.js (定义 window.TDC)
  -> 注入挑战全局 + setData 序列
  -> collect = decodeURIComponent(TDC.getData(true)), eks = TDC.getInfo().info
  -> POW: md5(prefix + decimal_u) == pow_cfg.md5
  -> cap_union_new_verify 表单提交
  -> 判定 errorCode=="0" && ticket 非空 && randstr 非空
"""
import base64
import hashlib
import json
import time
import urllib.parse
from pathlib import Path

import iv8
from curl_cffi import requests as creq

OUT = Path(__file__).resolve().parent / "tasks" / "zhilian-tdc-iv8"
OUT.mkdir(parents=True, exist_ok=True)

APPID = "25200697"
SID = "1343190090"
DEVICE_ID = "1018152636"
UUID = "4117903521339852012"
API = "https://captcha.eo.qq.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
UA_B64 = base64.b64encode(UA.encode()).decode()


def prehandle() -> dict:
    """GET cap_union_prehandle -> 同轮 sess/pow_cfg/tdc_path"""
    t = [
        f"aid={APPID}", "protocol=https", "accver=1", "showtype=popup",
        "ua=" + urllib.parse.quote(UA_B64, safe=""), "noheader=0", "fb=1",
        "aged=0", "enableAged=0", "enableDarkMode=0", f"deviceID={DEVICE_ID}",
        "passthrough=", f"uuid={UUID}", f"sid={SID}", "uid=",
        "grayscale=1", "dyeid=0", "clientype=2",
    ]
    hdr = {
        "user-agent": UA,
        "referer": "https://www.zhaopin.com/",
        "accept-language": "zh-CN,zh;q=0.9",
        "origin": "https://www.zhaopin.com",
        "accept": "*/*",
    }
    r = creq.get(API + "/cap_union_prehandle?" + "&".join(t), headers=hdr, impersonate="chrome", timeout=20)
    raw = r.text.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return json.loads(raw)


def download_tdc(tdc_path: str) -> str:
    """下载该轮动态 tdc.js"""
    url = tdc_path if tdc_path.startswith("http") else API + tdc_path
    hdr = {
        "user-agent": UA,
        "referer": "https://www.zhaopin.com/",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    r = creq.get(url, headers=hdr, impersonate="chrome", timeout=20)
    return r.text


def solve_pow(prefix: str, target: str) -> tuple[str, float, int]:
    """POW: md5(prefix + decimal_u) == target, 返回 (pow_answer, calc_time_ms, u)"""
    t0 = time.perf_counter()
    u = 0
    while True:
        if hashlib.md5(f"{prefix}{u}".encode()).hexdigest() == target:
            break
        u += 1
        if u > 50_000_000:
            raise RuntimeError(f"POW 搜索 5000 万次未命中 prefix={prefix}")
    dur = (time.perf_counter() - t0) * 1000
    return f"{prefix}{u}", round(dur, 1), u


def iv8_get_collect_eks(tdc_js: str, sess: str) -> tuple[str, str, dict]:
    """iv8 执行 tdc.js + setData 序列 -> (collect, eks, logs)"""
    logs = {}
    with iv8.JSContext() as ctx:
        # 挑战全局 (TDC 工作流要求的 iframe 环境)
        ctx.eval(
            f"""
            TCaptchaSid = "{sess}";
            TCaptchaReferrer = "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm";
            TCaptchaIframeClientPos = {{x: 128, y: 320, w: 320, h: 160}};
            window.name = "TCD" + Array.from({{length: 16}}, () => "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[Math.floor(Math.random()*36)]).join("");
            window.TCaptchaIsNewEntry = true;
            """
        )
        ctx.eval(tdc_js)
        logs["tdc_loaded"] = ctx.eval("typeof window.TDC")
        if ctx.eval("typeof window.TDC") != "object":
            raise RuntimeError("TDC 未定义: " + logs["tdc_loaded"])

        # setData 序列 (click_verify 分支, 从源码复刻)
        use_ft = "NO_FT" not in __import__("os").environ
        setdata = r"""
        TDC.setData({isNewEntry: 1});
        """ + (r"""
        TDC.setData({ft: "-----BEGIN FALLBACK-----"});   // ft 占位, 验证是否被消费
        """ if use_ft else "") + r"""
        TDC.setData("autoVerify", [{elem_id: 0, type: "DynAnswerType_TIME", data: ""}]);
        """
        try:
            ctx.eval(setdata)
            logs["setdata"] = "OK"
        except Exception as e:
            logs["setdata"] = str(e)[:200]

        collect_raw = ctx.eval("TDC.getData(true) || ''")
        eks = ctx.eval("(TDC.getInfo() || {}).info || ''")
        logs["collect_raw_len"] = len(collect_raw)
        logs["eks_len"] = len(eks)
        logs["TDC_keys"] = ctx.eval("Object.keys(window.TDC || {})")
        return collect_raw, eks, logs


def verify(sess: str, collect: str, eks: str, ans: str, pow_answer: str, pow_calc_time: float) -> dict:
    """POST cap_union_new_verify"""
    import os
    form = {
        "collect": collect,
        "tlg": len(collect),
        "eks": eks,
        "sess": sess,
        "ans": ans,
        "pow_answer": pow_answer,
        "pow_calc_time": pow_calc_time,
    }
    mode = os.environ.get("VERIFY_MODE", "iframe")
    if mode == "iframe":
        # 模拟验证码 iframe 视角 (真实 widget 从 iframe 发请求)
        hdr = {
            "user-agent": UA,
            "referer": "https://captcha.eo.gtimg.com/static/template/widget_ele_eo.html",
            "origin": "https://captcha.eo.gtimg.com",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "zh-CN,zh;q=0.9",
        }
    elif mode == "bare":
        hdr = {"user-agent": UA, "content-type": "application/x-www-form-urlencoded; charset=UTF-8"}
    else:
        hdr = {
            "user-agent": UA,
            "referer": "https://www.zhaopin.com/",
            "origin": "https://www.zhaopin.com",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "zh-CN,zh;q=0.9",
        }
    r = creq.post(API + "/cap_union_new_verify", data=form, headers=hdr, impersonate="chrome", timeout=20)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300], "_status": r.status_code}


def run_one() -> dict:
    d = prehandle()
    if d.get("state") != 1:
        return {"ok": False, "err": f"prehandle state={d.get('state')} error={d.get('error')}"}
    sess = d["sess"]
    cfg = d["data"]["comm_captcha_cfg"]
    dyn = d["data"]["dyn_show_info"]
    show_type = dyn.get("show_type", "?")

    tdc_js = download_tdc(cfg["tdc_path"])
    (OUT / f"tdc_{sess[:8]}.js").write_text(tdc_js, encoding="utf-8")

    collect_raw, eks, logs = iv8_get_collect_eks(tdc_js, sess)
    collect = urllib.parse.unquote(collect_raw)

    pow_cfg = cfg.get("pow_cfg") or {}
    pow_info = {}
    if pow_cfg.get("prefix") and pow_cfg.get("md5"):
        pow_answer, pow_time, pow_u = solve_pow(pow_cfg["prefix"], pow_cfg["md5"])
        pow_info = {
            "prefix": pow_cfg["prefix"],
            "target": pow_cfg["md5"],
            "u": pow_u,
            "md5_check": hashlib.md5(f"{pow_cfg['prefix']}{pow_u}".encode()).hexdigest() == pow_cfg["md5"],
        }
    else:
        pow_answer, pow_time = "", 0.0
        pow_u = -1

    ans_obj = [{"elem_id": 0, "type": "DynAnswerType_TIME", "data": ""}]
    resp = verify(sess, collect, eks, json.dumps(ans_obj), pow_answer, pow_time)

    result = {
        "ok": resp.get("errorCode") == "0",
        "sess": sess,
        "show_type": show_type,
        "errorCode": resp.get("errorCode"),
        "ticket_len": len(resp.get("ticket") or ""),
        "randstr_len": len(resp.get("randstr") or ""),
        "collect_len": len(collect),
        "eks_len": len(eks),
        "pow_answer": pow_answer[:20],
        "pow_calc_time_ms": pow_time,
        "pow_info": pow_info,
        "collect_sample": collect[:80],
        "logs": logs,
        "verify_resp": {k: v for k, v in resp.items() if k != "ticket"},
    }
    (OUT / f"result_{sess[:8]}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    for i in range(1, 3):
        print(f"\n===== Round {i} =====")
        r = run_one()
        print("sess:", r["sess"][:16], "| show_type:", r.get("show_type"))
        print("collect_len:", r["collect_len"], "| eks_len:", r["eks_len"])
        print("collect[:80]:", r.get("collect_sample", ""))
        print("POW:", r["pow_answer"][:16], "| u:", r["pow_info"].get("u"), "| md5_ok:", r["pow_info"].get("md5_check"), "| time", r["pow_calc_time_ms"], "ms")
        print("verify errorCode:", r.get("errorCode"), "| ticket_len:", r.get("ticket_len"), "| randstr_len:", r.get("randstr_len"))
        print("SETDATA LOG:", r["logs"].get("setdata"))
        print("PASS" if r["ok"] else "FAIL")
        if not r["ok"]:
            print("verify resp:", json.dumps(r["verify_resp"], ensure_ascii=False)[:300])
            break
        time.sleep(2)
