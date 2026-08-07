# -*- coding: utf-8 -*-
"""RiskState 风控状态机自检: 状态转移 / token 缓存 / 冷却 / 持久化."""
from __future__ import annotations

import time

from utils.risk import RiskState, STATE_OK, STATE_CHALLENGE, STATE_COOLING


def _make_risk(tmp_path):
    return RiskState(path=str(tmp_path / "risk.local.json"))


def test_state_default(tmp_path):
    r = _make_risk(tmp_path)
    assert r.state == STATE_OK
    assert r.challenge_streak == 0
    assert not r.is_cooling()
    assert r.remaining_cooldown() == 0
    assert r.rate_factor() == 1.0


def test_on_success_resets(tmp_path):
    r = _make_risk(tmp_path)
    r.on_challenge()
    r.on_challenge()
    assert r.challenge_streak == 2
    r.on_success()
    assert r.challenge_streak == 0
    assert r.state == STATE_OK
    assert r.rate_factor() == 1.0


def test_challenge_escalation(tmp_path):
    """连续 challenge 达到阈值进入冷却。"""
    r = _make_risk(tmp_path)
    for _ in range(2):
        r.on_challenge()
    assert r.state == STATE_CHALLENGE
    assert r.rate_factor() == 2.0
    r.on_challenge()  # 第三次 -> 冷却
    assert r.state == STATE_COOLING
    assert r.is_cooling()


def test_captcha_cooldown_exponential(tmp_path):
    r = _make_risk(tmp_path)
    r.on_captcha()
    assert r.captcha_count == 1
    assert 60 <= r.cooldown_sec <= 120
    assert r.is_cooling()
    # 第二次冷却时长翻倍
    r.cooling_until = 0  # 清掉当前冷却以便再触发
    r.on_captcha()
    assert r.captcha_count == 2
    assert r.cooldown_sec >= 120


def test_rate_factor_cooling(tmp_path):
    r = _make_risk(tmp_path)
    r.on_captcha()
    assert r.rate_factor() == 4.0  # challenge 2x * cooling 2x


def test_token_cache_and_expiry(tmp_path):
    r = _make_risk(tmp_path)
    assert r.get_token("https://x") is None
    r.set_token("https://x", "TOKEN123", ttl=3600)
    assert r.get_token("https://x") == "TOKEN123"
    # 过期后取不到
    r.set_token("https://x", "TOKEN123", ttl=0)
    assert r.get_token("https://x") is None


def test_persistence(tmp_path):
    p = tmp_path / "risk.local.json"
    r = RiskState(path=str(p))
    r.on_captcha()
    r.set_token("https://x", "PERSISTED", ttl=3600)
    r.save()
    # 新实例重新加载
    r2 = RiskState(path=str(p))
    assert r2.captcha_count == r.captcha_count
    assert r2.state == STATE_COOLING
    assert r2.get_token("https://x") == "PERSISTED"


def test_await_cooldown_blocks(tmp_path):
    r = _make_risk(tmp_path)
    r.on_captcha()
    # 把冷却窗口缩短到 0.2s
    r.cooldown_sec = 0.2
    r.cooling_until = time.time() + 0.2
    t0 = time.time()
    r.await_cooldown()
    assert time.time() - t0 >= 0.15  # 确实阻塞了
    assert not r.is_cooling()
