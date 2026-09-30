"""按 (caller, version) 组合的配额：跨版本跨调用方隔离、覆盖优先、并发精确、窗口切换。"""

import threading

import pytest

from gateway import QuotaLimiter, QuotaScope


def build() -> QuotaLimiter:
    q = QuotaLimiter()
    q.set_version_limit("v1", limit=2, window_seconds=60.0)
    q.set_version_limit("v2", limit=2, window_seconds=60.0)
    q.override_caller_limit("v1", "vip", limit=1)
    return q


def test_same_caller_quotas_isolated_across_versions():
    q = build()
    assert q.check("alice", "v1").allowed
    assert q.check("alice", "v1").allowed
    rejected = q.check("alice", "v1")
    assert not rejected.allowed
    assert rejected.scope is QuotaScope.VERSION
    # v1 打满不影响 v2
    d = q.check("alice", "v2")
    assert d.allowed and d.scope is QuotaScope.VERSION
    assert q.check("alice", "v2").allowed
    assert not q.check("alice", "v2").allowed


def test_callers_are_isolated_on_same_version():
    q = build()
    assert q.check("alice", "v1").allowed
    assert q.check("alice", "v1").allowed
    assert not q.check("alice", "v1").allowed
    assert q.check("bob", "v1").allowed
    assert q.check("carol", "v1").allowed


def test_caller_override_takes_precedence_and_reports_scope():
    q = build()
    assert q.effective_rule("vip", "v1").scope is QuotaScope.CALLER
    assert q.effective_rule("vip", "v1").limit == 1
    assert q.effective_rule("alice", "v1").scope is QuotaScope.VERSION
    assert q.check("vip", "v1").allowed
    rejected = q.check("vip", "v1")
    assert not rejected.allowed
    assert rejected.scope is QuotaScope.CALLER  # 命中专属覆盖
    assert rejected.remaining == 0
    assert rejected.retry_after is not None and rejected.retry_after > 0


def test_rejection_reports_scope_remaining_retry_after():
    now = [0.0]
    q = QuotaLimiter(clock=lambda: now[0])
    q.set_version_limit("v1", limit=2, window_seconds=60.0)
    q.check("alice", "v1")
    now[0] = 3.0
    q.check("alice", "v1")
    now[0] = 4.0
    d = q.check("alice", "v1")
    assert (d.allowed, d.remaining, d.scope, d.caller, d.version) == (
        False, 0, QuotaScope.VERSION, "alice", "v1",
    )
    assert d.retry_after == pytest.approx(56.0, abs=1e-9)  # 等 t=0 那条滑出


def test_concurrent_requests_never_exceed_quota_per_scope():
    """100 线程争抢 (alice, v1) 的 10 个配额：恰好放行 10，拒绝 90。"""
    q = QuotaLimiter()
    q.set_version_limit("v1", limit=10, window_seconds=60.0)
    decisions = []
    barrier = threading.Barrier(100)

    def hit():
        barrier.wait()
        decisions.append(q.check("alice", "v1"))

    threads = [threading.Thread(target=hit) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([d for d in decisions if d.allowed]) == 10
    rejected = [d for d in decisions if not d.allowed]
    assert len(rejected) == 90
    assert all(d.scope is QuotaScope.VERSION and d.remaining == 0 for d in rejected)
    assert sorted(d.remaining for d in decisions if d.allowed) == list(range(10))


def test_concurrent_isolation_between_scopes():
    """两个作用域各 5 个配额，各 40 线程：各自恰好放行 5，互不借用。"""
    q = QuotaLimiter()
    q.set_version_limit("v1", limit=5, window_seconds=60.0)
    q.set_version_limit("v2", limit=5, window_seconds=60.0)
    decisions = {"v1": [], "v2": []}

    def hit(version):
        decisions[version].append(q.check("alice", version))

    threads = []
    barrier = threading.Barrier(80)

    def run(version):
        barrier.wait()
        hit(version)

    for version in ("v1", "v2"):
        for _ in range(40):
            threads.append(threading.Thread(target=run, args=(version,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for version in ("v1", "v2"):
        allowed = [d for d in decisions[version] if d.allowed]
        assert len(allowed) == 5
        assert len([d for d in decisions[version] if not d.allowed]) == 35


def test_window_rollover_releases_exactly_expired_slots():
    """窗口半开：到期请求恰好释放，不多放不少算。"""
    now = [1000.0]
    q = QuotaLimiter(clock=lambda: now[0])
    q.set_version_limit("v1", limit=2, window_seconds=10.0)
    assert q.check("a", "v1").allowed          # t=1000
    now[0] = 1005.0
    assert q.check("a", "v1").allowed          # t=1005
    assert not q.check("a", "v1").allowed      # 满
    now[0] = 1010.0
    assert q.check("a", "v1").allowed          # 1000 那条恰好滑出
    assert not q.check("a", "v1").allowed      # 又满（1005、1010）
    now[0] = 1015.0
    assert q.check("a", "v1").allowed          # 1005 滑出（剩 1010、1015）


def test_override_does_not_affect_version_quota_of_others():
    q = build()
    # vip 用掉自己的专属额度，不消耗 v1 统一额度
    q.check("vip", "v1")
    assert not q.check("vip", "v1").allowed
    assert q.check("alice", "v1").allowed
    assert q.check("bob", "v1").allowed
