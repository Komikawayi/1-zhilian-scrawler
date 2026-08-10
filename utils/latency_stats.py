# -*- coding: utf-8 -*-
"""
分环节耗时统计收集器 — 实时均值 + 结束总结表

采集指标 (2026-08-08 确定, 见 docs/zhilian-crawler-pipeline.md §3/§4):
  - 网络请求: 总耗时 total + TTFB (STARTTRANSFER_TIME), 只采这两个。
    不采 DNS/TCP/TLS —— keep-alive 稳态下每连接只发生一次 ≈ 0,
    是链路/系统属性非采集可控, 采了只会污染统计误导分析。
  - 本地处理: 解析 / DB 写入 / 冷却等待 (每次请求都发生, 用于证明
    木桶在服务端还是本地)。

用法:
  stats = LatencyStats()
  stats.record_net("search", total_s, ttfb_s)   # 网络请求打点 (stage ∈ search/detail)
  stats.record("detail_parse", seconds)         # 本地环节打点
  stats.current_mean("search_net")              # 实时显示累计均值
  stats.print_summary(worker_id)                # 结束总结表 (stdout + 日志)
"""
from __future__ import annotations

import logging
import unicodedata
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# 每 stage 样本上限 (deque 有界窗口): 超出后只保留最近 N 条。
# 百万级任务下若不限, 详情 4 stage 全量样本可达 ~150MB/worker, 逼近 OOM;
# 窗口内均值/p50/p95 已足够定位木桶。
MAX_SAMPLES = 50000

# 结束总结表显示顺序 + 中文标签
_STAGE_ORDER = [
    "search_net", "detail_net",          # 网络 (总耗时 + TTFB 附加列)
    "search_parse", "detail_parse",      # 本地解析
    "db_detail", "db_search",            # 本地入库
    "risk_check", "cooldown",            # 风控冷却等待
]
_STAGE_LABEL = {
    "search_net": "搜索网络", "detail_net": "详情网络",
    "search_parse": "搜索解析", "detail_parse": "详情解析",
    "db_detail": "详情入库", "db_search": "搜索入库",
    "risk_check": "risk precheck", "cooldown": "冷却等待",
}


def _cjk_pad(text: str, width: int) -> str:
    """CJK-aware 左对齐补全: 中文字符按 2 半角宽计, 保证终端等宽下对齐。"""
    n = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
    return text + " " * max(0, width - n)


class LatencyStats:
    """分环节耗时统计 (asyncio 单线程内无锁; 多 worker 各自实例)。"""

    def __init__(self) -> None:
        self._stages: Dict[str, Deque[float]] = {}   # 通用环节 -> ms 有界窗口
        self._ttfb: Dict[str, Deque[float]] = {}     # 网络环节 TTFB -> ms 有界窗口

    # ---- 打点 ----

    def record(self, stage: str, seconds: float) -> None:
        """本地环节打点 (解析/DB/冷却等)。seconds 秒, 内部转 ms。"""
        self._stages.setdefault(stage, deque(maxlen=MAX_SAMPLES)).append(seconds * 1000)

    def record_net(self, stage: str, total: Optional[float],
                   ttfb: Optional[float]) -> None:
        """网络请求打点: stage ∈ {"search","detail"} → 存 {stage}_net 总耗时 + TTFB。"""
        if total is None:
            return
        self._stages.setdefault(f"{stage}_net", deque(maxlen=MAX_SAMPLES)).append(total * 1000)
        if ttfb is not None:
            self._ttfb.setdefault(stage, deque(maxlen=MAX_SAMPLES)).append(ttfb * 1000)

    # ---- 实时查询 ----

    def current_mean(self, stage: str) -> Optional[float]:
        """累计均值 ms (实时显示用); 支持 'search_net' / 'search_ttfb' / 本地环节。"""
        if stage.endswith("_net"):
            vals = self._stages.get(stage)
        elif stage.endswith("_ttfb"):
            vals = self._ttfb.get(stage[:-5])          # "search_ttfb" -> "search"
        else:
            vals = self._stages.get(stage)
        return (sum(vals) / len(vals)) if vals else None

    # ---- 总结 ----

    @staticmethod
    def _pct(vals, p: float) -> float:
        """分位数: 排序后取索引 int(n*p); 样本=1 时 p50=p95=max=该值。"""
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(int(len(s) * p), len(s) - 1)]

    def summary(self) -> List[dict]:
        """每环节统计: count/mean/p50/p95/max; 网络环节附 ttfb_mean。"""
        rows = []
        for stage in _STAGE_ORDER:
            vals = self._stages.get(stage)
            if not vals:
                continue
            base = stage[:-4] if stage.endswith("_net") else stage
            ttfb_vals = self._ttfb.get(base)
            rows.append({
                "stage": stage,
                "label": _STAGE_LABEL.get(stage, stage),
                "count": len(vals),
                "mean": sum(vals) / len(vals),
                "p50": self._pct(vals, 0.5),
                "p95": self._pct(vals, 0.95),
                "max": max(vals),
                "ttfb_mean": (sum(ttfb_vals) / len(ttfb_vals)) if ttfb_vals else None,
            })
        return rows

    def format_table(self, worker_id: int = 0) -> str:
        """结束总结表 (真表头 + 固定列宽 + CJK 对齐)。"""
        rows = self.summary()
        lines = [f"=== worker {worker_id} 分环节耗时统计 ==="]
        lines.append("  " + _cjk_pad("环节", 10) + _cjk_pad("样本", 6) +
                     _cjk_pad("均值", 8) + _cjk_pad("p50", 8) +
                     _cjk_pad("p95", 8) + _cjk_pad("max", 8) + "  " + _cjk_pad("(TTFB)", 12))
        if not rows:
            lines.append("  (无样本)")
            return "\n".join(lines)
        for r in rows:
            ttfb = f"({r['ttfb_mean']:.0f}ms)" if r["ttfb_mean"] is not None else "-"
            lines.append(
                "  " + _cjk_pad(r["label"], 10) + str(r["count"]).rjust(6) +
                f"{r['mean']:.0f}ms".rjust(8) + f"{r['p50']:.0f}ms".rjust(8) +
                f"{r['p95']:.0f}ms".rjust(8) + f"{r['max']:.0f}ms".rjust(8) +
                "  " + _cjk_pad(ttfb, 12))
        return "\n".join(lines)

    def print_summary(self, worker_id: int = 0, to_stdout: bool = True) -> None:
        """结束总结: 终端 print + 日志 INFO 双通道 (采集完成时调用一次)。

        to_stdout=False 时仅写日志 —— 多 worker 下非 worker0 进程不 print 终端,
        避免与 worker0 实时进度显示器的 ANSI 覆盖交叠花屏 (日志每进程独立文件保留全量)。
        """
        text = self.format_table(worker_id)
        if to_stdout:
            print(text)
        logger.info("分环节耗时统计:\n%s", text)
