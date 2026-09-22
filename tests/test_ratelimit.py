"""限流：并发下配额准确，窗口边界不漏放、不重复计数。"""

import threading

from gateway import SlidingWindowRateLimiter


def test_concurrent_requests_never_exceed_quota():
    """100 个线程争抢 10 个配额：放行数必须恰好等于配额。"""
    limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60.0)
    decisions = []
    barrier = threading.Barrier(100)

    def hit():
        barrier.wait()
        decisions.append(limiter.check("alice"))

    threads = [threading.Thread(target=hit) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = [d for d in decisions if d.allowed]
    rejected = [d for d in decisions if not d.allowed]
    assert len(allowed) == 10
    assert len(rejected) == 90
    assert all(d.retry_after is not None and d.retry_after > 0 for d in rejected)
    assert sorted(d.remaining for d in allowed) == list(range(10))


def test_callers_are_isolated():
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    assert limiter.check("alice").allowed
    assert not limiter.check("alice").allowed
    assert limiter.check("bob").allowed


def test_window_boundary_is_half_open():
    """t=0 的请求在 t=window 时恰好过期：边界不重复计数也不漏放。"""
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0, clock=lambda: now[0])
    assert limiter.check("a").allowed          # t=1000
    now[0] = 1005.0
    assert limiter.check("a").allowed          # t=1005
    assert not limiter.check("a").allowed      # 满
    now[0] = 1009.999
    assert not limiter.check("a").allowed      # 窗口内仍满
    now[0] = 1010.0
    assert limiter.check("a").allowed          # t=1000 那条滑出，放行
    assert not limiter.check("a").allowed      # 又满（t=1005 与 t=1010 两条）


def test_retry_after_points_to_earliest_expiry():
    now = [0.0]
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0, clock=lambda: now[0])
    limiter.check("a")          # t=0
    now[0] = 3.0
    limiter.check("a")          # t=3
    now[0] = 4.0
    decision = limiter.check("a")
    assert not decision.allowed
    assert decision.retry_after == 6.0  # 等 t=0 那条滑出
