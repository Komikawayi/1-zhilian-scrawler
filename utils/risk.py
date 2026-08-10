# -*- coding: utf-8 -*-
"""
智联采集器 — 风控状态管理 (IP 信誉状态机 + 自适应退避 + token 缓存)

把"检测防线变化 → 记录 → 退避 → 冷却 → 恢复"变成跨 run 持久化的状态机,
而不是每个请求各自为战、进程退出即丢。

防线分级 (与 docs/zhilian-edgeone-reverse-analysis.md §4 一致):
    ok(直通) → challenge(JS挑战, 可本地解) → captcha(交互验证码, 需冷却) → cooling

持久化: legacy 路径使用本地 JSON；分布式路径使用 Redis 按出口共享状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 状态文件路径 (gitignored via config/*.local.json)
DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "zhilian-risk.local.json",
)

# 防线等级
STATE_OK = "ok"          # 直通
STATE_CHALLENGE = "challenge"   # JS Challenge (可解, 但说明信誉在降)
STATE_CAPTCHA = "captcha"       # 交互验证码 (信誉差, 需冷却)
STATE_COOLING = "cooling"       # 正在冷却

# 升级阈值: 连续遇到 challenge 多少次升级到 captcha 档 (触发冷却)
CHALLENGE_ESCALATION = 3

# 冷却基础 (秒), 指数递增: 60, 120, 240, ... 上限 30min
COOLDOWN_BASE = 60
COOLDOWN_CAP = 1800

# 自适应速率: 升级时把请求间隔乘上这个因子 (配合 http_client)
ESCALATION_RATE_FACTOR = 2.0


class RiskState:
    """跨 run 持久化的风控状态机。

    Usage:
        risk = RiskState()            # 自动 load config/zhilian-risk.local.json
        risk.await_cooldown()         # 冷却期阻塞等待
        # 请求后分类喂回:
        risk.on_success() | risk.on_challenge() | risk.on_captcha()
        risk.set_token(url, token)    # 缓存挑战 token (1h 复用)
        token = risk.get_token(url)   # 命中则复用
        risk.save()                   # 落盘
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_STATE_PATH
        self._lock = threading.Lock()
        self.state: str = STATE_OK
        self.challenge_streak: int = 0      # 连续 challenge 计数
        self.captcha_count: int = 0         # 累计验证码次数 (用于指数冷却)
        self.cooling_until: float = 0.0     # unix 时间戳, 冷却结束
        self.cooldown_sec: float = 0.0      # 本次冷却时长
        self.tokens: Dict[str, Dict] = {}   # {url: {"token": str, "expires_at": float}}
        self._load()

    # ---- 加载/保存 ----

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.state = d.get("state", STATE_OK)
            self.challenge_streak = d.get("challenge_streak", 0)
            self.captcha_count = d.get("captcha_count", 0)
            self.cooling_until = d.get("cooling_until", 0)
            self.cooldown_sec = d.get("cooldown_sec", 0)
            self.tokens = d.get("tokens", {})
            logger.info("风控状态加载: state=%s challenge_streak=%d captcha_count=%d",
                        self.state, self.challenge_streak, self.captcha_count)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("风控状态读取失败 %s: %s", self.path, e)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "state": self.state,
            "challenge_streak": self.challenge_streak,
            "captcha_count": self.captcha_count,
            "cooling_until": self.cooling_until,
            "cooldown_sec": self.cooldown_sec,
            "tokens": self.tokens,
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- 状态喂回 ----

    def on_success(self) -> None:
        """直通/拿到数据: 信誉良好, 重置升级计数。"""
        with self._lock:
            self.state = STATE_OK
            self.challenge_streak = 0
        # 成功不清冷却计数, 冷却期间的成功不解除冷却 (以冷却时间为准)

    def on_challenge(self) -> None:
        """遇到 JS Challenge: 信誉在降, 连续计数达到阈值升级。"""
        with self._lock:
            self.challenge_streak += 1
            if self.challenge_streak >= CHALLENGE_ESCALATION:
                self._enter_cooling_unlocked(reason="连续 %d 次 challenge" % self.challenge_streak)
            else:
                self.state = STATE_CHALLENGE

    def on_captcha(self) -> None:
        """交互验证码: 信誉差, 直接进入指数冷却。"""
        with self._lock:
            self._enter_cooling_unlocked(reason="交互验证码")

    def _enter_cooling(self, reason: str) -> None:
        with self._lock:
            self._enter_cooling_unlocked(reason)

    def _enter_cooling_unlocked(self, reason: str) -> None:
        self.state = STATE_COOLING
        self.captcha_count += 1
        self.cooldown_sec = min(COOLDOWN_BASE * (2 ** (self.captcha_count - 1)), COOLDOWN_CAP)
        self.cooling_until = time.time() + self.cooldown_sec
        logger.warning("进入冷却 %ds (%s, 第 %d 次), 至 %s",
                       int(self.cooldown_sec), reason, self.captcha_count,
                       time.strftime("%H:%M:%S", time.localtime(self.cooling_until)))

    # ---- 查询 ----

    def is_cooling(self) -> bool:
        if self.state == STATE_COOLING and time.time() < self.cooling_until:
            return True
        return False

    def remaining_cooldown(self) -> float:
        """剩余冷却秒数 (未在冷却返回 0)。"""
        if self.is_cooling():
            return max(0.0, self.cooling_until - time.time())
        return 0.0

    def await_cooldown(self) -> None:
        """阻塞直到冷却结束 (供采集主流程调用)。"""
        sec = self.remaining_cooldown()
        if sec > 0:
            logger.info("风控冷却中, 等待 %d 秒", int(sec))
            time.sleep(sec)

    def rate_factor(self) -> float:
        """自适应速率因子: challenge 期间拉大间隔, 直通回到 1.0。"""
        if self.state == STATE_CHALLENGE:
            return ESCALATION_RATE_FACTOR
        if self.state == STATE_COOLING:
            return ESCALATION_RATE_FACTOR * 2.0
        return 1.0

    # ---- token 缓存 (EO-Bot-Js-Token 1h 复用) ----

    def get_token(self, url: str) -> Optional[str]:
        """返回未过期缓存的 token; 无/过期返回 None。"""
        t = self.tokens.get(url)
        if not t:
            return None
        if time.time() >= t.get("expires_at", 0):
            return None
        return t.get("token")

    def set_token(self, url: str, token: str, ttl: float = 3600) -> None:
        """缓存 token (默认 1h, 匹配 EdgeOne max-age=3600)。"""
        self.tokens[url] = {"token": token, "expires_at": time.time() + ttl}
        logger.debug("缓存 token: url=%s len=%d ttl=%ds", url[:60], len(token), int(ttl))

    # ---- async 支持 (Phase A 高并发流水线) ----

    async def async_await_cooldown(self) -> None:
        """阻塞直到冷却结束 (async 版, 分片睡眠不阻塞事件循环, 可响应取消)。"""
        while True:
            sec = self.remaining_cooldown()
            if sec <= 0:
                return
            logger.info("风控冷却中, 等待 %d 秒", int(sec))
            await asyncio.sleep(min(sec, 5.0))

    async def async_on_success(self) -> None:
        self.on_success()

    async def async_on_challenge(self) -> None:
        self.on_challenge()

    async def async_on_captcha(self) -> None:
        self.on_captcha()


class AsyncRedisRiskState:
    """跨 worker 共享的异步风控状态。

    状态转移在 Redis Lua 中完成，多个进程看到同一出口 IP 的冷却窗口。
    ``scope`` 应对应实际出口 IP 或代理身份，多出口时不能共用一个 scope。
    """

    _CHALLENGE_LUA = """
local streak = tonumber(redis.call('HGET', KEYS[1], 'challenge_streak') or '0') + 1
if streak >= tonumber(ARGV[1]) then
  local count = tonumber(redis.call('HGET', KEYS[1], 'captcha_count') or '0') + 1
  local cooldown = math.min(tonumber(ARGV[2]) * (2 ^ (count - 1)), tonumber(ARGV[3]))
  local clock = redis.call('TIME')
  local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
  local until_ts = now + cooldown
  redis.call('HSET', KEYS[1], 'state', 'cooling', 'challenge_streak', streak,
            'captcha_count', count, 'cooldown_sec', cooldown, 'cooling_until', until_ts)
  return 'cooling'
end
redis.call('HSET', KEYS[1], 'state', 'challenge', 'challenge_streak', streak)
return 'challenge'
"""

    _CAPTCHA_LUA = """
local count = tonumber(redis.call('HGET', KEYS[1], 'captcha_count') or '0') + 1
local cooldown = math.min(tonumber(ARGV[1]) * (2 ^ (count - 1)), tonumber(ARGV[2]))
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local until_ts = now + cooldown
redis.call('HSET', KEYS[1], 'state', 'cooling', 'captcha_count', count,
          'cooldown_sec', cooldown, 'cooling_until', until_ts)
return 'cooling'
"""

    _SUCCESS_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
local cooling_until = tonumber(redis.call('HGET', KEYS[1], 'cooling_until') or '0')
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
if state == 'cooling' and cooling_until > now then
  return 'cooling'
end
redis.call('HSET', KEYS[1], 'state', 'ok', 'challenge_streak', 0)
return 'ok'
"""

    def __init__(self, redis, scope: str = "default") -> None:
        self.redis = redis
        self.key = f"zhaopin:risk:{scope}"
        self.state = STATE_OK

    async def _ensure(self) -> None:
        await self.redis.hsetnx(self.key, "state", STATE_OK)
        await self.redis.hsetnx(self.key, "challenge_streak", 0)
        await self.redis.hsetnx(self.key, "captcha_count", 0)
        await self.redis.hsetnx(self.key, "cooling_until", 0)
        await self.redis.hsetnx(self.key, "cooldown_sec", 0)

    async def _refresh(self) -> dict:
        await self._ensure()
        values = await self.redis.hgetall(self.key)
        self.state = values.get("state", STATE_OK)
        return values

    async def async_await_cooldown(self) -> None:
        while True:
            values = await self._refresh()
            until = float(values.get("cooling_until", 0) or 0)
            clock = await self.redis.time()
            server_now = float(clock[0]) + float(clock[1]) / 1000000
            sec = until - server_now
            if values.get("state") != STATE_COOLING or sec <= 0:
                return
            logger.info("共享风控冷却中, 等待 %d 秒", int(sec))
            await asyncio.sleep(min(sec, 5.0))

    async def async_on_success(self) -> None:
        self.state = await self.redis.eval(self._SUCCESS_LUA, 1, self.key)

    async def async_on_challenge(self) -> None:
        self.state = await self.redis.eval(
            self._CHALLENGE_LUA, 1, self.key, CHALLENGE_ESCALATION,
            COOLDOWN_BASE, COOLDOWN_CAP)

    async def async_on_captcha(self) -> None:
        self.state = await self.redis.eval(
            self._CAPTCHA_LUA, 1, self.key, COOLDOWN_BASE,
            COOLDOWN_CAP)
