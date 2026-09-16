# -*- coding: utf-8 -*-
"""真实协议测试: 端到端验证采集链路 (需网络, 会真实请求智联)。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.http_client import ZhilianClient  # noqa: E402
from utils.parser import extract_initial_state, parse_positions  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(os.environ.get("ZHAOPIN_NETWORK_TEST"), "设置 ZHAOPIN_NETWORK_TEST=1 启用真实网络测试")
class TestProtocol(unittest.TestCase):
    def test_fetch_and_parse(self):
        client = ZhilianClient(min_interval=0.3, max_interval=0.6)
        html = client.fetch_search_page("python", "530", 1)
        state = extract_initial_state(html)
        rows = parse_positions(state)
        self.assertGreater(len(rows), 0)
        self.assertEqual(state["pageIndex"], 1)

    def test_pagination_differs(self):
        client = ZhilianClient(min_interval=0.3, max_interval=0.6)
        h1 = client.fetch_search_page("python", "530", 1)
        h2 = client.fetch_search_page("python", "530", 2)
        s1 = extract_initial_state(h1)
        s2 = extract_initial_state(h2)
        self.assertEqual(s1["pageIndex"], 1)
        self.assertEqual(s2["pageIndex"], 2)
        n1 = [r["position_number"] for r in parse_positions(s1)]
        n2 = [r["position_number"] for r in parse_positions(s2)]
        self.assertNotEqual(n1, n2, "两页职位不应完全相同")


if __name__ == "__main__":
    unittest.main()
