"""按"调用方 × 接口版本"组合生效的滑动窗口配额。

作用范围：
- 同一个调用方在不同版本上的额度相互独立；不同调用方之间互不影响。
- 每个版本可设置统一额度（VERSION 范围，对该版本所有调用方生效）；
  也可为特定调用方单独覆盖（CALLER 范围，优先于版本统一额度）。
- 命中哪条配额范围会随拒绝结论返回，调用方可区分是
  "该调用方在该版本的专属额度"还是"该版本的统一额度"超限。

计数与判定在同一把锁内原子完成，窗口为半开区间 (now-window, now]，
并发下放行数量严格等于额度，窗口切换不多放、不少算。
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class QuotaScope(enum.Enum):
    """命中的配额范围。"""

    CALLER = "CALLER"    # 某调用方在某版本上的专属覆盖额度
    VERSION = "VERSION"  # 某版本的统一额度


@dataclass(frozen=True)
class QuotaRule:
    """一条生效配额。"""

    limit: int
    window_seconds: float
    scope: QuotaScope


@dataclass(frozen=True)
class QuotaDecision:
    """配额判定结果。"""

    allowed: bool
    remaining: int
    retry_after: float | None
    scope: QuotaScope
    limit: int
    caller: str
    version: str


@dataclass
class _Window:
    window_seconds: float
    hits: deque[float]


class QuotaLimiter:
    """按 (caller, version) 独立计数的配额限流器。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # version -> 统一额度（window, limit）
        self._version_limits: dict[str, tuple[int, float]] = {}
        # version -> caller -> 专属覆盖额度（window 与该版本统一额度一致）
        self._caller_limits: dict[str, dict[str, int]] = {}
        # (caller, version) -> 命中窗口
        self._windows: dict[tuple[str, str], _Window] = {}

    def set_version_limit(self, version: str, limit: int, window_seconds: float) -> None:
        """设置某版本的统一额度，对该版本所有未被单独覆盖的调用方生效。"""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._version_limits[version] = (limit, window_seconds)

    def override_caller_limit(self, version: str, caller: str, limit: int) -> None:
        """为某调用方在某版本上单独覆盖额度，优先于版本统一额度。

        窗口时长沿用该版本统一额度配置，因此必须先调用 set_version_limit。
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if version not in self._version_limits:
            raise ValueError(
                f"set version limit for {version!r} before overriding caller limit"
            )
        self._caller_limits.setdefault(version, {})[caller] = limit

    def effective_rule(self, caller: str, version: str) -> QuotaRule:
        """返回当前对该 (caller, version) 生效的配额规则；未配置则抛 KeyError。"""
        if version in self._version_limits:
            _, window = self._version_limits[version]
            overrides = self._caller_limits.get(version)
            if overrides is not None and caller in overrides:
                return QuotaRule(overrides[caller], window, QuotaScope.CALLER)
            limit, _ = self._version_limits[version]
            return QuotaRule(limit, window, QuotaScope.VERSION)
        raise KeyError(f"no quota configured for version {version!r}")

    def check(self, caller: str, version: str, now: float | None = None) -> QuotaDecision:
        """判定并（若放行）记录一次调用；整个过程原子完成。"""
        if now is None:
            now = self._clock()
        with self._lock:
            rule = self.effective_rule(caller, version)
            win = self._windows.get((caller, version))
            if win is None or win.window_seconds != rule.window_seconds:
                win = _Window(rule.window_seconds, deque())
                self._windows[(caller, version)] = win
            dq = win.hits
            cutoff = now - rule.window_seconds
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= rule.limit:
                retry_after = dq[0] + rule.window_seconds - now
                return QuotaDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=max(retry_after, 0.0),
                    scope=rule.scope,
                    limit=rule.limit,
                    caller=caller,
                    version=version,
                )
            dq.append(now)
            return QuotaDecision(
                allowed=True,
                remaining=rule.limit - len(dq),
                retry_after=None,
                scope=rule.scope,
                limit=rule.limit,
                caller=caller,
                version=version,
            )
