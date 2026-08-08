# -*- coding: utf-8 -*-
"""固定输入自检: 用 js_reverse_cache 里的侦察样本验证解析逻辑 (不依赖网络)。"""
from __future__ import annotations

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.parser import (  # noqa: E402
    clean_job_desc, extract_initial_state, parse_meta, parse_position_detail_v2,
    parse_positions,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_GLOBS = [
    os.path.join(BASE, "js_reverse_cache", "assets", "html", "*.html"),
    os.path.join(BASE, "js_reverse_cache", "assets", "html", "curl_cffi_success.html"),
]


def load_any_html() -> str:
    for g in HTML_GLOBS:
        files = glob.glob(g)
        if files:
            with open(files[0], encoding="utf-8") as f:
                return f.read()
    raise FileNotFoundError("js_reverse_cache/html/ 下没有样本 HTML，请先运行侦察工具")


class TestParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load_any_html()

    def test_extract_initial_state(self):
        state = extract_initial_state(self.html)
        self.assertIsInstance(state, dict)
        self.assertIn("positionList", state)

    def test_parse_positions(self):
        state = extract_initial_state(self.html)
        rows = parse_positions(state)
        self.assertGreater(len(rows), 0)
        row = rows[0]
        for key in ("position_name", "salary_display", "company_name", "city_district"):
            self.assertIn(key, row, f"缺少字段 {key}")
        # 至少一条有职位名
        self.assertTrue(any(r["position_name"] for r in rows))

    def test_parse_meta(self):
        state = extract_initial_state(self.html)
        meta = parse_meta(state)
        self.assertIsInstance(meta["position_count"], int)
        self.assertIsInstance(meta["page_index"], int)

    def test_malformed_html(self):
        with self.assertRaises(ValueError):
            extract_initial_state("<html>no state here</html>")


# ---- clean_job_desc: job_desc HTML → 纯文本 ----

def test_clean_br_to_newline():
    assert clean_job_desc("a<br>b") == "a\nb"


def test_clean_div_block_lines():
    assert clean_job_desc("<div>  1)职责</div><div>  2)职责</div>") == "1)职责\n2)职责"


def test_clean_consecutive_breaks_merged():
    assert clean_job_desc("岗位职责：<br><br>任职要求：") == "岗位职责：\n任职要求："


def test_clean_list():
    assert clean_job_desc("<ul><li>甲</li><li>乙</li></ul>") == "甲\n乙"


def test_clean_nbsp_entity():
    assert clean_job_desc("a&nbsp;b") == "a b"


def test_clean_no_leading_trailing_newline():
    assert clean_job_desc("<div>内容</div>") == "内容"


def test_clean_plain_text_passthrough():
    assert clean_job_desc("没有标签的纯文本") == "没有标签的纯文本"


def test_clean_empty_and_none():
    assert clean_job_desc("") == ""
    assert clean_job_desc(None) == ""


def test_parse_v2_job_desc_cleaned():
    """parse_position_detail_v2 输出 job_desc 已是纯文本。"""
    data = {"detailedPosition": {"positionName": "测试", "jobDesc": "职责1<br>职责2"},
            "detailedCompany": {}, "taskId": "t1"}
    row = parse_position_detail_v2(data)
    assert row["job_desc"] == "职责1\n职责2"


if __name__ == "__main__":
    unittest.main()
