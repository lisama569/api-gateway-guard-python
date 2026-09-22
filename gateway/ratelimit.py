"""按调用方维度的滑动窗口限流器。

语义：
- 窗口为半开区间 (now - window, now]，边界上的时间戳视为已过期，
  避免窗口边界抖动造成漏放或重复计数。
- 计数与判定在同一把锁内完成，并发请求下配额准确。
- 拒绝时返回 retry_after：窗口内最早一次请求滑出窗口所需等待的秒数，
  调用方可据此判断重试时机。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结果。"""

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
