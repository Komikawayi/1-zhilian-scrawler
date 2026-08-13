# -*- coding: utf-8 -*-
"""Parser tests with synthetic SSR data and no captured page content."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.parser import clean_job_desc, extract_initial_state, parse_meta, parse_position_detail_v2, parse_positions

SAMPLE_HTML = '''<script>__INITIAL_STATE__={"positionList":[{"name":"Sample Role","number":"sample-1","salary60":"10k-15k","companyName":"Sample Company","cityDistrict":"Sample District"}],"positionCount":1,"pageIndex":1,"pageSize":20,"pages":1}</script>'''


class TestParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SAMPLE_HTML

    def test_extract_initial_state(self):
        state = extract_initial_state(self.html)
        self.assertIsInstance(state, dict)
        self.assertIn("positionList", state)

    def test_parse_positions(self):
        rows = parse_positions(extract_initial_state(self.html))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in ("position_name", "salary_display", "company_name", "city_district"):
            self.assertIn(key, row, f"缺少字段 {key}")
        self.assertEqual(row["position_name"], "Sample Role")

    def test_parse_meta(self):
        meta = parse_meta(extract_initial_state(self.html))
        self.assertEqual(meta["position_count"], 1)
        self.assertEqual(meta["page_index"], 1)

    def test_malformed_html(self):
        with self.assertRaises(ValueError):
            extract_initial_state("<html>no state here</html>")


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
    data = {"detailedPosition": {"positionName": "测试", "jobDesc": "职责1<br>职责2"}, "detailedCompany": {}, "taskId": "t1"}
    row = parse_position_detail_v2(data)
    assert row["job_desc"] == "职责1\n职责2"


if __name__ == "__main__":
    unittest.main()
