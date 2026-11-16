"""公开演示模式下的防刷闸门：按 IP 的分钟级限流 + 每日总量上限。

为什么要它
----------
线上这个 Demo 同样是**无鉴权**的（任何人拿到 IP 就能问），而问数比文档问答更贵：
一次提问要走 intake → linker → sqlcraft → critic → reporter 多个节点，
在 api 模式下是 4~6 次模型调用、上万 token。所以"被刷"的后果比 DocuMind 更重。

设计取舍
--------
1. **不做鉴权**：HR 点开链接就要能问。限制的是速率与总量，不是身份。
2. **三层，而不是一层**：
   - 按 IP 每分钟：挡脚本（高并发、固定 IP）；
   - 按 IP 每天：挡单个访客把额度用光；
   - 全站每天：挡换 IP / 分布式刷法，损失封顶。
3. **纯内存**：重启清零。演示场景下比引入 Redis 划算。
4. **fail-open**：限流器自身异常时放行。宁可少挡一次，也不要让一个 bug 把
   整个 Demo 变成 500。
5. **只拦问数接口**：``/api/ask`` 与 ``/api/ask/stream``。
   ``/api/stats`` 是前端每几秒轮询一次的，``/api/traces`` 是展示用的，
   拦了会让页面自己把自己刷死。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict


def _int_env(name: str, default: int) -> int:
    """读整型环境变量；写错了退回默认值而不是崩掉。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


class RateLimiter:
    """按 IP 的滑动窗口 + 每日计数。

    ``clock`` 可注入，测试里不用真等 60 秒就能验证窗口滚动。
    """

    def __init__(
        self,
        per_min: int = 3,
        per_day: int = 10,
        daily_total: int = 40,
        window_s: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        self.per_min = per_min
        self.per_day = per_day
        self.daily_total = daily_total
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._day_start = clock()
        self._total_today = 0
        self._total_by_ip: dict[str, int] = defaultdict(int)

    # -- 内部 ---------------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        if now - self._day_start >= 86400:
            self._day_start = now
            self._total_today = 0
            self._total_by_ip.clear()
            self._hits.clear()

    def _recent(self, ip: str, now: float) -> list[float]:
        hits = [t for t in self._hits[ip] if now - t < self.window_s]
        if hits:
            self._hits[ip] = hits
        else:
            self._hits.pop(ip, None)
        return hits

    # -- 对外 ---------------------------------------------------------------
    def check(self, ip: str) -> tuple[bool, int, str]:
        """返回 ``(是否放行, Retry-After 秒, 原因)``。放行时不计数。

        check / hit 分两步：参数校验失败的请求不该消耗额度。
        """
        now = self._clock()
        self._roll_day(now)

        if self.daily_total and self._total_today >= self.daily_total:
            return (False, 3600, f"今日全站额度已用完（{self.daily_total} 次），明天再来")

        if self.per_day and self._total_by_ip[ip] >= self.per_day:
            return (False, 3600, f"你的今日额度已用完（{self.per_day} 次）")

        if self.per_min:
            recent = self._recent(ip, now)
            if len(recent) >= self.per_min:
                wait = max(1, int(self.window_s - (now - recent[0])) + 1)
                return (False, wait, f"请求过于频繁（每分钟最多 {self.per_min} 次）")

        return (True, 0, "")

    def hit(self, ip: str) -> None:
        self._hits[ip].append(self._clock())
        self._total_today += 1
        self._total_by_ip[ip] += 1

    def snapshot(self) -> dict:
        return {
            "per_min": self.per_min or None,
            "per_day": self.per_day or None,
            "daily_total": self.daily_total or None,
            "used_today": self._total_today,
            "window_s": self.window_s,
        }


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def build_from_env() -> RateLimiter:
    """从环境变量构造。

    默认值比 DocuMind 更紧：一次问数要跑 4~6 次模型调用（上万 token），
    1M 的免费额度只够几十次。10 次/天/IP 对真实访客够用，对刷子不够。

    ``IP_RATE_ENABLED=0`` 时全部归零（= 不限）。测试套件里用它关掉闸门，
    否则几十条用例共用一个进程级计数器，跑到第 4 条就开始 429。
    限流本身另有 ``tests/test_limits.py`` 专门覆盖，关掉不影响那部分。
    """
    if not _env_flag("IP_RATE_ENABLED", True):
        return RateLimiter(per_min=0, per_day=0, daily_total=0)
    return RateLimiter(
        per_min=_int_env("IP_RATE_PER_MIN", 3),
        per_day=_int_env("IP_RATE_PER_DAY", 10),
        daily_total=_int_env("IP_RATE_DAILY_TOTAL", 40),
    )
