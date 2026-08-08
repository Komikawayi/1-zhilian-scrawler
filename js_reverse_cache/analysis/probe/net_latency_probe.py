# -*- coding: utf-8 -*-
"""
网络延迟分段探测 — 对比 sou 搜索页 vs fe-api 详情接口

背景: 实测搜索平均 ~460ms, 详情 ~185ms, 差异 ~275ms。本脚本分段定位
差异环节: DNS / TCP connect / TLS 握手 / TTFB(首字节=服务端处理) / 下载 / 总耗时。

用 curl_cffi 底层 Curl API 的 CURLINFO 计时字段, 走系统代理(与采集一致)。
用法:  PYTHONIOENCODING=utf-8 python js_reverse_cache/analysis/probe/net_latency_probe.py
"""
from __future__ import annotations

import time
from curl_cffi import Curl, CurlOpt
from curl_cffi.const import CurlInfo

# ---- 目标 ----
SEARCH_URL = "https://sou.zhaopin.com/?kw=SMT&p=1"
DETAIL_URL = ("https://fe-api.zhaopin.com/c/i/jobs/position-detailv2"
              "?_v=0.12345678&x-zp-page-request-id=probe&x-zp-client-id=probe"
              "&platform=13&version=0.0.0&number=CCL1480117890J40614881205")
DETAIL_HEADERS = [
    b"accept: application/json, text/plain, */*",
    b"x-zp-business-system: 1",
    b"x-zp-page-code: 4019",
    b"x-zp-platform: 13",
    b"referer: https://www.zhaopin.com/",
]


def _noop(buf: bytes) -> int:
    """丢弃响应体/响应头 (模块级引用, 避免 C 回调 handle 被 GC)。"""
    return len(buf)


def probe_once(url: str, headers: list[str] | None = None) -> dict:
    """单次请求返回分段计时 (秒) + 响应体大小。"""
    c = Curl()
    c.setopt(CurlOpt.URL, url)
    c.setopt(CurlOpt.IMPERSONATE, "chrome131")  # requests 层 "chrome" 的默认映射
    c.setopt(CurlOpt.FOLLOWLOCATION, True)       # 采集实际行为: 搜索页 302 -> SSR
    # 丢弃响应体, 避免底层 Curl 默认把 body 打到 stdout; 只保留计时
    c.setopt(CurlOpt.WRITEFUNCTION, _noop)
    c.setopt(CurlOpt.HEADERFUNCTION, _noop)
    c.setopt(CurlOpt.TIMEOUT_MS, 25000)
    if headers:
        c.setopt(CurlOpt.HTTPHEADER, headers)
    t0 = time.perf_counter()
    c.perform()
    wall = time.perf_counter() - t0
    g = c.getinfo
    result = {
        "dns": g(CurlInfo.NAMELOOKUP_TIME),
        "tcp": g(CurlInfo.CONNECT_TIME),
        "tls": g(CurlInfo.APPCONNECT_TIME),
        "ttfb": g(CurlInfo.STARTTRANSFER_TIME),   # 首字节 = 服务端处理 + 链路
        "total": g(CurlInfo.TOTAL_TIME),
        "download": g(CurlInfo.SIZE_DOWNLOAD_T),
        "wall": wall,
        "http": c.getinfo(CurlInfo.RESPONSE_CODE),
    }
    c.close()
    return result


def probe_warm(url: str, headers: list[bytes] | None, n: int = 3) -> list[float]:
    """requests.Session 连接池复用 (贴近采集稳态): 预热建连后连测 n 次, 返回 ms 列表。"""
    import curl_cffi.requests as rq
    hdr = {k.split(b":", 1)[0].decode(): k.split(b":", 1)[1].strip().decode()
           for k in (headers or [])} or None
    s = rq.Session(impersonate="chrome")
    s.get(url, headers=hdr, timeout=25)  # 预热: 建连 + TLS
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        s.get(url, headers=hdr, timeout=25)
        times.append((time.perf_counter() - t0) * 1000)
    s.close()
    return times


def summarize(label: str, url: str, headers: list[bytes] | None, n: int = 5):
    rows = [probe_once(url, headers) for _ in range(n)]
    n = len(rows)
    def avg(key):
        return sum(r[key] for r in rows if r[key] is not None) / n
    dns, tcp, tls, ttfb, total = avg("dns"), avg("tcp"), avg("tls"), avg("ttfb"), avg("total")
    dl_kb = avg("download") / 1024
    http = rows[0]["http"]
    print(f"\n=== {label} ===")
    print(f"  HTTP {http}   响应体 {dl_kb:.0f} KB")
    print(f"  DNS     {dns*1000:7.1f} ms")
    print(f"  TCP     {(tcp-dns)*1000:7.1f} ms   (cum {tcp*1000:.1f})")
    print(f"  TLS     {(tls-tcp)*1000:7.1f} ms   (cum {tls*1000:.1f})")
    print(f"  TTFB    {(ttfb-tls)*1000:7.1f} ms  <-- 服务端处理+往返, 差异主要在这")
    print(f"  下载    {(total-ttfb)*1000:7.1f} ms   (cum {total*1000:.1f})")
    print(f"  总计    {total*1000:7.1f} ms   (wall {sum(r['wall'] for r in rows)/n*1000:.0f})")
    # 热连接 (requests.Session keep-alive 复用, 贴近采集稳态)
    warm = probe_warm(url, headers)
    print(f"  -- 热连接(keep-alive复用, 贴近采集稳态): " + " ".join(f"{t:.0f}ms" for t in warm) +
          f"  平均 {sum(warm)/len(warm):.0f}ms")
    return rows


if __name__ == "__main__":
    summarize("搜索 sou.zhaopin.com SSR 页", SEARCH_URL, None)
    summarize("详情 fe-api detailv2 JSON", DETAIL_URL, DETAIL_HEADERS)
