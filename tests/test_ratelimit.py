"""限流测试：并发配额准确、超限原因与重试信息可区分、窗口边界不重不漏。"""

from __future__ import annotations

import concurrent.futures as cf
import threading

import pytest

from app.ratelimit import RateLimiter
from tests.conftest import log_request


class FakeClock:
    """线程安全的可控时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def set(self, value: float) -> None:
        with self._lock:
            self._now = value


def test_concurrent_over_limit_is_exact() -> None:
    clock = FakeClock()
    rl = RateLimiter(limit=10, window_seconds=60, clock=clock)

    log_request("rate-concurrent", request_id="req-rl-conc", caller="acme", threads=100, limit=10)
    with cf.ThreadPoolExecutor(max_workers=32) as pool:
        decisions = list(pool.map(lambda _: rl.check("acme"), range(100)))

    allowed = [d for d in decisions if d.allowed]
    denied = [d for d in decisions if not d.allowed]
    # 并发下恰好放行 10 次：不漏放（不会 >10），也不多计数（不会 <10）
    assert len(allowed) == 10
    assert len(denied) == 90
    # 结论一致、原因可区分
    assert {d.reason for d in allowed} == {"OK"}
    assert {d.reason for d in denied} == {"QUOTA_EXCEEDED"}
    assert all(d.remaining == 0 for d in denied)
    assert all(d.retry_after >= 1 for d in denied)


def test_callers_are_counted_independently() -> None:
    clock = FakeClock()
    rl = RateLimiter(limit=2, window_seconds=60, clock=clock)
    assert rl.check("a").allowed
    assert rl.check("a").allowed
    denied_for_a = rl.check("a")
    log_request(
        "rate-per-caller",
        request_id="req-rl-caller",
        caller_a_allowed=denied_for_a.allowed,
        caller_b_check="independent",
    )
    assert not denied_for_a.allowed
    # b 的配额不受 a 影响
    assert rl.check("b").allowed


def test_window_rollover_resets_quota_exactly() -> None:
    clock = FakeClock()
    rl = RateLimiter(limit=3, window_seconds=10, clock=clock)
    for _ in range(3):
        assert rl.check("c").allowed
    assert not rl.check("c").allowed

    # 跨过边界前一刻仍被拒，不会因抖动提前放行
    clock.set(9.999)
    log_request("rate-boundary", request_id="req-rl-edge", now=9.999, expect="denied")
    assert not rl.check("c").allowed

    # 进入新窗口立即恢复满配额
    clock.set(10.0)
    d = rl.check("c")
    log_request("rate-boundary", request_id="req-rl-edge2", now=10.0, window_epoch=d.window_epoch)
    assert d.allowed
    assert d.remaining == 2
    assert d.window_epoch == 10


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(limit=0, window_seconds=10)
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=0)
