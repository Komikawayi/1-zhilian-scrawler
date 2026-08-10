"""run.py 配置到 collect.py 命令行的映射测试。"""
from unittest.mock import patch

import run
from config import settings


def test_runtime_defaults_match_requested_targets():
    assert run.DEFAULT_SEARCH_CONCURRENCY == settings.SEARCH_CONCURRENCY == 100
    assert run.DEFAULT_CONCURRENCY == settings.DETAIL_CONCURRENCY == 400
    assert run.DEFAULT_SEARCH_RATE == settings.SEARCH_RATE_PER_SEC == 100.0
    assert run.DEFAULT_RATE == settings.DETAIL_RATE_PER_SEC == 400.0


def test_redis_runner_forwards_split_limits():
    conf = {
        "keywords": ["smt"],
        "cities": ["653"],
        "workers": 2,
        "concurrency": 17,
        "search_concurrency": 9,
        "search_rate": 18,
        "detail_rate": 123,
        "clear": False,
        "name": "test",
    }
    with patch("run.subprocess.run") as invoke:
        invoke.return_value.returncode = 0
        assert run._run_redis(conf) == 0

    consume = invoke.call_args_list[1].args[0]
    assert consume[consume.index("--concurrency") + 1] == "17"
    assert consume[consume.index("--search-concurrency") + 1] == "9"
    assert consume[consume.index("--search-rate") + 1] == "18"
    assert consume[consume.index("--detail-rate") + 1] == "123"


def test_single_runner_selects_single_mode():
    conf = {
        "keywords": ["smt"], "cities": ["653"], "pages": 2,
        "concurrency": 3, "rate": 8, "name": "test",
    }
    with patch("run.subprocess.run") as invoke:
        invoke.return_value.returncode = 0
        assert run._run_single(conf) == 0
    assert "--single" in invoke.call_args.args[0]
