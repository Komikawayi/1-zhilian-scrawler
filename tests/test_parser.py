# -*- coding: utf-8 -*-
"""固定输入自检: 用 js_reverse_cache 里的侦察样本验证解析逻辑 (不依赖网络)。"""
from __future__ import annotations

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.parser import extract_initial_state, parse_positions, parse_meta  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
