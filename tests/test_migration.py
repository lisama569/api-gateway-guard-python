"""端到端：版本退场 + 跨版本配额 + 跨版本幂等在 GatewayService 上协同一致。"""

import datetime
import threading

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    QuotaLimiter,
    QuotaScope,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayService


@pytest.fixture
def service() -> GatewayService:
    versions = VersionRegistry()
    versions.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset_on=datetime.date(2027, 3, 1),
            successor="v2",
        )
    )
    versions.register(
        VersionRule("v2", required_fields=frozenset({"name", "email"})),
    )
    versions.register(VersionRule("v0", status=VersionStatus.RETIRED, successor="v2"))
    quota = QuotaLimiter()
    quota.set_version_limit("v1", limit=3, window_seconds=60.0)
    quota.set_version_limit("v2", limit=3, window_seconds=60.0)
    quota.override_caller_limit("v2", "vip", limit=1)
    return GatewayService(versions=versions, limiter=quota, idempotency=IdempotencyStore())


def req(i, *, caller="alice", version="v1", key=None, payload=None) -> GatewayRequest:
    return GatewayRequest(
        request_id=f"req-{i}",
        caller=caller,
        version=version,
        idempotency_key=key or f"k-{i}",
        payload=payload if payload is not None else {"name": f"n{i}"},
    )


def v2_payload(name="a"):
    return {"name": name, "email": f"{name}@x.io"}


# ---------- 生命周期 ----------

def test_deprecated_version_served_with_warning(service):
    resp = service.handle(req(1), lambda p: {"ok": True})
    assert resp.status == "executed"
    assert resp.deprecation is not None
    assert resp.warnings[0]["successor"] == "v2"
    assert resp.warnings[0]["sunset_on"] == "2027-03-01"


def test_deprecated_replay_keeps_warning(service):
    service.handle(req(1, key="same", payload={"name": "fixed"}), lambda p: "r")
    resp = service.handle(req(2, key="same", payload={"name": "fixed"}), lambda p: "r")
    assert resp.status == "replayed"
    assert resp.deprecation is not None


def test_retired_version_rejected_distinctly(service):
    with pytest.raises(GatewayError) as exc:
        service.handle(req(1, version="v0"), lambda p: p)
    assert exc.value.code is ErrorCode.VERSION_RETIRED


def test_missing_and_malformed_version_distinct(service):
    with pytest.raises(GatewayError) as exc:
        service.handle(req(1, version=None), lambda p: p)
    assert exc.value.code is ErrorCode.VERSION_MISSING
    with pytest.raises(GatewayError) as exc:
        service.handle(req(2, version="V1"), lambda p: p)
    assert exc.value.code is ErrorCode.VERSION_MALFORMED


def test_retired_consumes_neither_quota_nor_idempotency(service):
    with pytest.raises(GatewayError):
        service.handle(req(1, version="v0", key="k"), lambda p: p)
    # v1 仍有完整 3 个配额；键 k 未被占用
    for i in range(3):
        assert service.handle(req(10 + i, key=f"other-{i}"), lambda p: p).status == "executed"


# ---------- 跨版本配额 ----------

def test_quota_is_independent_per_version(service):
    # 打满 alice 在 v1 的 3 个
    for i in range(3):
        service.handle(req(i, key=f"k{i}"), lambda p: p)
    with pytest.raises(GatewayError) as exc:
        service.handle(req(9, key="k9"), lambda p: p)
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.scope == QuotaScope.VERSION.value
    assert exc.value.remaining == 0
    # v2 额度不受 v1 影响
    resp = service.handle(req(20, version="v2", key="k20", payload=v2_payload("a")), lambda p: p)
    assert resp.status == "executed"


def test_quota_is_independent_per_caller(service):
    for i in range(3):
        service.handle(req(i, caller="alice", key=f"ka{i}"), lambda p: p)
    with pytest.raises(GatewayError):
        service.handle(req(9, caller="alice", key="ka9"), lambda p: p)
    # bob 的额度独立
    assert service.handle(req(30, caller="bob", key="kb0"), lambda p: p).status == "executed"


def test_caller_override_scope_reported(service):
    service.handle(req(1, caller="vip", version="v2", key="v1", payload=v2_payload()), lambda p: p)
    with pytest.raises(GatewayError) as exc:
        service.handle(req(2, caller="vip", version="v2", key="v2", payload=v2_payload("b")), lambda p: p)
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.scope == QuotaScope.CALLER.value
    assert exc.value.retry_after is not None and exc.value.retry_after > 0
    # 普通调用方仍走版本统一额度（3 个），不受 vip 覆盖影响
    for i in range(3):
        assert service.handle(
            req(10 + i, caller="bob", version="v2", key=f"bk{i}", payload=v2_payload(str(i))),
            lambda p: p,
        ).status == "executed"


def test_concurrent_quota_per_scope_is_exact(service):
    """v1 与 v2 各 3 个配额，60 个并发请求按版本各恰好放行 3。"""
    outcomes = []
    barrier = threading.Barrier(60)

    def run(i):
        version = "v1" if i < 30 else "v2"
        payload = {"name": f"n{i}"} if version == "v1" else v2_payload(f"n{i}")
        barrier.wait()
        try:
            service.handle(req(i, version=version, key=f"cc-{i}", payload=payload), lambda p: p)
            outcomes.append((version, "ok"))
        except GatewayError as e:
            outcomes.append((version, e.code))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for version in ("v1", "v2"):
        ok = [o for o in outcomes if o == (version, "ok")]
        limited = [o for o in outcomes if o == (version, ErrorCode.RATE_LIMITED)]
        assert len(ok) == 3
        assert len(limited) == 27


# ---------- 跨版本幂等 ----------

def test_same_key_isolated_between_versions(service):
    service.handle(req(1, key="same", payload={"name": "a"}), lambda p: {"from": "v1"})
    # 换版本使用同一键：全新执行，不回放 v1 结果
    resp = service.handle(
        req(2, version="v2", key="same", payload=v2_payload("a")),
        lambda p: {"from": "v2"},
    )
    assert resp.status == "executed"
    assert resp.result == {"from": "v2"}
    # 各自作用域内重复仍分别回放各自结果
    assert service.handle(req(3, key="same", payload={"name": "a"}), lambda p: {"from": "v1"}).result == {"from": "v1"}
    replay_v2 = service.handle(req(4, version="v2", key="same", payload=v2_payload("a")), lambda p: {"from": "v2"})
    assert replay_v2.status == "replayed" and replay_v2.result == {"from": "v2"}


def test_same_key_isolated_between_callers(service):
    service.handle(req(1, caller="alice", key="same", payload={"name": "a"}), lambda p: {"by": "alice"})
    resp = service.handle(req(2, caller="bob", key="same", payload={"name": "a"}), lambda p: {"by": "bob"})
    assert resp.status == "executed" and resp.result == {"by": "bob"}
    # bob 完成后 alice 仍是自己的结果
    replay = service.handle(req(3, caller="alice", key="same", payload={"name": "a"}), lambda p: None)
    assert replay.status == "replayed" and replay.result == {"by": "alice"}


def test_cross_version_same_key_no_false_conflict(service):
    service.handle(req(1, key="same", payload={"name": "a"}), lambda p: 1)
    # 不同版本 + 不同请求体形状，也不应被判成幂等冲突
    resp = service.handle(
        req(2, version="v2", key="same", payload=v2_payload("zzz")),
        lambda p: 2,
    )
    assert resp.status == "executed"


def test_failure_retry_is_scoped(service):
    def boom(_):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        service.handle(req(1, key="same", payload={"name": "a"}), boom)
    # 同作用域安全重试
    resp = service.handle(req(2, key="same", payload={"name": "a"}), lambda p: {"recovered": True})
    assert resp.result == {"recovered": True}
    # 其他版本/调用方不受失败记录影响
    assert service.handle(
        req(3, version="v2", key="same", payload=v2_payload("a")), lambda p: {"fresh": True}
    ).status == "executed"
