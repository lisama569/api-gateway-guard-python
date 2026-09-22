"""按调用方维度的固定窗口限流（并发安全）。

语义（明确、可解释）：

* 以调用方（``caller``）为维度，每个固定时间窗口最多放行 ``limit`` 次；
* "判定 + 计数" 在同一把锁内原子完成，并发请求下不会漏放（超发），
  同一次请求也不会重复计数；
* 窗口滚动使用整数窗口下标计算，边界处一次性原子切换，杜绝临界点上的
  抖动重复计数；
* 被限流的请求返回结构化结论：原因、当前窗口起点与 ``retry_after``
  （距下个窗口的秒数），调用方可据此决定重试时机。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("app.ratelimit")


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结论。"""

    allowed: bool
    caller: str
    limit: int
    remaining: int
    window_epoch: int        # 当前窗口起点（秒）
    retry_after: int          # 建议重试等待秒数（被拒时为窗口剩余时间）
    reason: str               # 机器可读原因


class RateLimiter:
    """固定时间窗口计数器。

    内部状态：caller -> (窗口起点epoch, 窗口内已计数)。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit 必须为正整数")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须为正整数")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def check(self, caller: str) -> RateLimitDecision:
        """判定一次请求是否放行，并对本次判定计数一次。"""
        now = self._clock()
        # 整数窗口下标 -> 窗口起点；边界处只可能整体落在新窗口，不会撕裂
        window_index = int(now // self.window_seconds)
        window_epoch = window_index * self.window_seconds

        # 读-改-写在同一临界区内：并发下计数严格准确
        with self._lock:
            current = self._windows.get(caller)
            if current is None or current[0] != window_epoch:
                # 新窗口（或首次访问）：原子地从 0 开始计数
                count = 0
            else:
                count = current[1]

            if count >= self.limit:
                elapsed = int(now) - window_epoch
                retry_after = max(1, self.window_seconds - elapsed)
                decision = RateLimitDecision(
                    allowed=False,
                    caller=caller,
                    limit=self.limit,
                    remaining=0,
                    window_epoch=window_epoch,
                    retry_after=retry_after,
                    reason="QUOTA_EXCEEDED",
                )
                logger.warning(
                    "rate-limit caller=%s allowed=False reason=QUOTA_EXCEEDED "
                    "used=%s limit=%s window_epoch=%s retry_after=%s",
                    caller,
                    count,
                    self.limit,
                    window_epoch,
                    retry_after,
                )
                return decision

            count += 1
            self._windows[caller] = (window_epoch, count)

        decision = RateLimitDecision(
            allowed=True,
            caller=caller,
            limit=self.limit,
            remaining=self.limit - count,
            window_epoch=window_epoch,
            retry_after=0,
            reason="OK",
        )
        logger.info(
            "rate-limit caller=%s allowed=True reason=OK used=%s/%s "
            "window_epoch=%s remaining=%s",
            caller,
            count,
            self.limit,
            window_epoch,
            decision.remaining,
        )
        return decision
