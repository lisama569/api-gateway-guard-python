"""按调用方维度的滑动窗口限流器。

语义：
- 窗口为半开区间 (now - window, now]，边界上的时间戳视为已过期，
  避免窗口边界抖动造成漏放或重复计数。
- 计数与判定在同一把锁内完成，并发请求下配额准确。
- 拒绝时返回 retry_after：窗口内最早一次请求滑出窗口所需等待的秒数，
  调用方可据此判断重试时机。

版本迁移期配额（ScopedSlidingWindowRateLimiter）：
- 配额作用域为 (caller, version) 组合：同一调用方在不同版本上额度独立，
  不同调用方之间也互不影响。
- 每个版本可设统一额度；特定调用方可在特定版本上单独覆盖。
- 拒绝时返回命中的配额范围（caller/version/limit/来源）与剩余次数。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结果（按调用方维度，旧版契约）。"""

    allowed: bool
    remaining: int
    retry_after: float | None


class SlidingWindowRateLimiter:
    """滑动窗口限流器，按 caller 维度独立计数。"""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, caller: str, now: float | None = None) -> RateLimitDecision:
        """判定并（若放行）记录一次调用；整个过程原子完成。"""
        if now is None:
            now = self._clock()
        with self._lock:
            dq = self._hits.setdefault(caller, deque())
            cutoff = now - self.window_seconds
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                retry_after = dq[0] + self.window_seconds - now
                return RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=max(retry_after, 0.0),
                )
            dq.append(now)
            return RateLimitDecision(
                allowed=True,
                remaining=self.limit - len(dq),
                retry_after=None,
            )


# ---- 按 (调用方, 版本) 组合作用域的配额策略 -------------------------------

# 配额命中范围来源：
# caller_version -> 某调用方在某版本上的单独覆盖
# version        -> 该版本的统一额度
# default        -> 全局兜底额度
QuotaScope = tuple[str, str]


@dataclass(frozen=True)
class QuotaPolicy:
    """配额策略。

    - default_limit：未配置任何规则时的兜底额度。
    - version_limits：version -> 该版本统一额度。
    - caller_version_limits：(caller, version) -> 特定调用方在该版本上的覆盖。
    优先级：caller_version_limits > version_limits > default_limit。
    """

    default_limit: int
    version_limits: dict[str, int] | None = None
    caller_version_limits: dict[QuotaScope, int] | None = None


@dataclass(frozen=True)
class ScopedRateLimitDecision:
    """按 (caller, version) 作用域的限流判定结果。"""

    allowed: bool
    scope: QuotaScope
    limit: int
    remaining: int
    retry_after: float | None
    #: 命中的规则来源："caller_version" | "version" | "default"
    source: str


class ScopedSlidingWindowRateLimiter:
    """按 (caller, version) 组合独立计数的滑动窗口限流器。"""

    def __init__(
        self,
        policy: QuotaPolicy,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy.default_limit <= 0:
            raise ValueError("default_limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        for label, limits in (
            ("version_limits", policy.version_limits or {}),
            ("caller_version_limits", policy.caller_version_limits or {}),
        ):
            bad = {k: lim for k, lim in limits.items() if lim <= 0}
            if bad:
                raise ValueError(f"{label} limits must be positive, got: {bad}")
        self._policy = policy
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[QuotaScope, deque[float]] = {}

    def limit_for(self, caller: str, version: str) -> tuple[int, str]:
        """返回某作用域命中的额度与规则来源。"""
        overrides = self._policy.caller_version_limits or {}
        if (caller, version) in overrides:
            return overrides[(caller, version)], "caller_version"
        version_limits = self._policy.version_limits or {}
        if version in version_limits:
            return version_limits[version], "version"
        return self._policy.default_limit, "default"

    def check(
        self, caller: str, version: str, now: float | None = None
    ) -> ScopedRateLimitDecision:
        """在 (caller, version) 作用域内判定并（若放行）记录一次调用。"""
        if now is None:
            now = self._clock()
        limit, source = self.limit_for(caller, version)
        scope: QuotaScope = (caller, version)
        with self._lock:
            dq = self._hits.setdefault(scope, deque())
            cutoff = now - self.window_seconds
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry_after = dq[0] + self.window_seconds - now
                return ScopedRateLimitDecision(
                    allowed=False,
                    scope=scope,
                    limit=limit,
                    remaining=0,
                    retry_after=max(retry_after, 0.0),
                    source=source,
                )
            dq.append(now)
            return ScopedRateLimitDecision(
                allowed=True,
                scope=scope,
                limit=limit,
                remaining=limit - len(dq),
                retry_after=None,
                source=source,
            )
