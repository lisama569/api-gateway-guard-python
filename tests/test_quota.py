"""按 (caller, version) 组合生效的配额：隔离、覆盖优先级、窗口切换与并发严格性。"""

import threading

import pytest

from gateway import QuotaPolicy, ScopedSlidingWindowRateLimiter


def build(clock=lambda: 0.0):
    # default=10；v2 统一 2；alice 在 v1 上单独覆盖为 1
    policy = QuotaPolicy(
        default_limit=10,
        version_limits={"v2": 2},
        caller_version_limits={("alice", "v1"): 1},
    )
    return ScopedSlidingWindowRateLimiter(policy, window_seconds=10.0, clock=clock)


def test_same_caller_quota_is_independent_across_versions():
    rl = build()
    # alice 在 v1 上只有 1
    assert rl.check("alice", "v1").allowed
    rejected = rl.check("alice", "v1")
    assert not rejected.allowed
    assert rejected.source == "caller_version"
    assert rejected.scope == ("alice", "v1")
    # v1 满了不影响 alice 在 v2（统一额度 2）
    d1 = rl.check("alice", "v2")
    d2 = rl.check("alice", "v2")
    assert d1.allowed and d2.allowed
    assert d1.source == "version" and d2.source == "version"
    assert not rl.check("alice", "v2").allowed
    # v3 没有版本规则，走 default=10，前两个版本都满也不影响
    d = rl.check("alice", "v3")
    assert d.allowed and d.source == "default" and d.limit == 10


def test_quota_is_independent_across_callers():
    rl = build()
    assert rl.check("alice", "v2").allowed
    assert rl.check("alice", "v2").allowed
    assert not rl.check("alice", "v2").allowed
    # 另一调用方同版本额度独立
    assert rl.check("bob", "v2").allowed
    # bob 没有 caller_version 覆盖，命中版本统一额度
    assert rl.check("bob", "v2").source == "version"


def test_caller_override_takes_precedence_over_version_limit():
    rl = build()
    # v1 没有版本规则（default=10），但 alice 被单独压到 1
    d = rl.check("alice", "v1")
    assert d.source == "caller_version" and d.limit == 1 and d.remaining == 0
    # 其他调用方在 v1 仍是默认 10
    assert rl.check("carol", "v1").source == "default"
    assert rl.check("carol", "v1").limit == 10


def test_rejection_carries_scope_remaining_and_retry_after():
    now = [0.0]
    rl = build(clock=lambda: now[0])
    rl.check("alice", "v2")
    now[0] = 3.0
    rl.check("alice", "v2")
    now[0] = 4.0
    d = rl.check("alice", "v2")
    assert not d.allowed
    assert d.scope == ("alice", "v2")
    assert d.limit == 2
    assert d.remaining == 0
    assert d.retry_after == pytest.approx(6.0)  # 等 t=0 那条滑出


def test_window_rollover_does_not_over_admit_or_under_count():
    """窗口半开边界：过期请求滑出后额度精确恢复，不多放不少算。"""
    now = [1000.0]
    rl = build(clock=lambda: now[0])
    assert rl.check("alice", "v2").allowed     # t=1000
    now[0] = 1005.0
    assert rl.check("alice", "v2").allowed     # t=1005
    assert not rl.check("alice", "v2").allowed
    now[0] = 1009.999
    assert not rl.check("alice", "v2").allowed
    now[0] = 1010.0                            # t=1000 恰好滑出
    d = rl.check("alice", "v2")
    assert d.allowed and d.remaining == 0      # t=1005 + 本条已满
    assert not rl.check("alice", "v2").allowed
    # 窗口滚动不影响另一作用域（bob/v2 此前没打过）
    assert rl.check("bob", "v2").remaining == 1


def test_concurrent_requests_admit_exactly_quota_per_scope():
    """同一作用域 100 线程抢 2 个版本统一配额：恰好放行 2。"""
    rl = build()
    decisions = []
    barrier = threading.Barrier(100)

    def hit():
        barrier.wait()
        decisions.append(rl.check("alice", "v2"))

    threads = [threading.Thread(target=hit) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = [d for d in decisions if d.allowed]
    rejected = [d for d in decisions if not d.allowed]
    assert len(allowed) == 2
    assert len(rejected) == 98
    assert sorted(d.remaining for d in allowed) == [0, 1]
    assert all(d.retry_after is not None and d.retry_after >= 0 for d in rejected)
    assert {d.scope for d in decisions} == {("alice", "v2")}


def test_concurrent_requests_across_versions_each_scope_independent():
    """v1（alice 覆盖=1）与 v2（统一=2）并发：各自严格按自己配额放行。"""
    rl = build()
    results: dict[str, list] = {"v1": [], "v2": []}
    lock = threading.Lock()
    barrier = threading.Barrier(60)

    def hit(version: str):
        barrier.wait()
        d = rl.check("alice", version)
        with lock:
            results[version].append(d)

    threads = (
        [threading.Thread(target=hit, args=("v1",)) for _ in range(30)]
        + [threading.Thread(target=hit, args=("v2",)) for _ in range(30)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(d.allowed for d in results["v1"]) == 1
    assert sum(d.allowed for d in results["v2"]) == 2
    assert all(d.scope == ("alice", "v1") for d in results["v1"])
    assert all(d.scope == ("alice", "v2") for d in results["v2"])


# ---- 服务端到端：配额拒绝携带命中范围 --------------------------------------

from gateway import (  # noqa: E402
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    VersionRegistry,
    VersionRule,
)
from gateway.service import GatewayRequest, GatewayService  # noqa: E402


def test_service_quota_rejection_details_identify_scope():
    versions = VersionRegistry()
    versions.register(VersionRule("v1", required_fields=frozenset({"name"})))
    versions.register(VersionRule("v2", required_fields=frozenset({"name"})))
    policy = QuotaPolicy(
        default_limit=100,
        caller_version_limits={("alice", "v1"): 1},
    )
    svc = GatewayService(
        versions=versions,
        limiter=ScopedSlidingWindowRateLimiter(policy, 60.0),
        idempotency=IdempotencyStore(),
    )

    def mk(i, version, caller="alice"):
        return GatewayRequest(
            request_id=f"r{i}", caller=caller, version=version,
            idempotency_key=f"k{i}", payload={"name": f"n{i}"},
        )

    svc.handle(mk(1, "v1"), lambda p: p)
    with pytest.raises(GatewayError) as exc:
        svc.handle(mk(2, "v1"), lambda p: p)
    err = exc.value
    assert err.code is ErrorCode.RATE_LIMITED
    assert err.details["scope"] == {"caller": "alice", "version": "v1"}
    assert err.details["limit"] == 1
    assert err.details["remaining"] == 0
    assert err.details["matched_rule"] == "caller_version"
    assert err.retry_after is not None and err.retry_after > 0

    # v1 超限不影响 alice 的 v2
    resp = svc.handle(mk(3, "v2"), lambda p: "v2-ok")
    assert resp.result == "v2-ok"
    # 也不影响另一调用方的 v1
    resp2 = svc.handle(mk(4, "v1", caller="bob"), lambda p: "bob-ok")
    assert resp2.result == "bob-ok"
