"""调用写法兼容性：升级前的位置参数构造与升级后的关键字构造必须结论一致。

背景回归：GatewayRequest 字段顺序在治理层升级时调整过，老调用方按
(request_id, caller, version, idempotency_key, payload) 位置传参时，
version 落到了 payload 槽位，被误判为 INVALID_VERSION 整体拒掉。
本文件锁定：同一份请求内容，两种等价写法的结论（含各失败分支）完全一致。
"""

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    QuotaPolicy,
    ScopedSlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayService

PAYLOAD_V2 = {"name": "a", "email": "e@x"}


@pytest.fixture
def service() -> GatewayService:
    registry = VersionRegistry()
    registry.register(VersionRule("v0", status=VersionStatus.RETIRED, replacement="v2"))
    registry.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset="2027-03-01",
            replacement="v2",
        )
    )
    registry.register(VersionRule("v2", required_fields=frozenset({"name", "email"})))
    return GatewayService(
        versions=registry,
        limiter=ScopedSlidingWindowRateLimiter(
            policy=QuotaPolicy(default_limit=100), window_seconds=60.0
        ),
        idempotency=IdempotencyStore(),
    )


def legacy_request(rid, caller, version, key, payload):
    """升级前的既有写法：按位置传 (request_id, caller, version, idempotency_key, payload)。"""
    return GatewayRequest(rid, caller, version, key, payload)


def modern_request(rid, caller, version, key, payload):
    """升级后的写法：按参数名逐个传。"""
    return GatewayRequest(
        request_id=rid, caller=caller, version=version,
        idempotency_key=key, payload=payload,
    )


def test_legacy_positional_construction_normalizes_to_same_fields():
    legacy = legacy_request("r1", "alice", "v2", "k1", PAYLOAD_V2)
    modern = modern_request("r1", "alice", "v2", "k1", PAYLOAD_V2)
    assert legacy == modern
    assert legacy.version == "v2"
    assert legacy.idempotency_key == "k1"
    assert legacy.payload == PAYLOAD_V2


def test_legacy_and_modern_success_conclusions_identical(service):
    results = []
    for i, build in enumerate((legacy_request, modern_request)):
        req = build("r-ok", "alice", "v2", f"k-ok-{i}", PAYLOAD_V2)
        resp = service.handle(req, handler=lambda p: {"echo": p})
        results.append((resp.status, resp.result, resp.deprecation))
    assert results[0] == results[1]
    assert results[0][0] == "executed"


def test_same_key_across_styles_shares_idempotency_scope(service):
    """两种写法构造的同键请求落在同一幂等作用域：先执行、后回放。"""
    first = service.handle(
        legacy_request("r-a", "alice", "v2", "k-shared", PAYLOAD_V2),
        handler=lambda p: {"echo": p},
    )
    assert first.status == "executed"
    second = service.handle(
        modern_request("r-b", "alice", "v2", "k-shared", PAYLOAD_V2),
        handler=lambda p: pytest.fail("同键同作用域不应再次执行"),
    )
    assert second.status == "replayed"
    assert second.result == first.result


def test_legacy_and_modern_replay_conclusions_identical(service):
    for build in (legacy_request, modern_request):
        first = build("r-a", "alice", "v2", "k-replay", PAYLOAD_V2)
        second = build("r-b", "alice", "v2", "k-replay", PAYLOAD_V2)
        service.handle(first, handler=lambda p: {"echo": p})
        replayed = service.handle(second, handler=lambda p: pytest.fail("不应再次执行"))
        assert replayed.status == "replayed"


@pytest.mark.parametrize(
    "version,payload,expected_code",
    [
        (None, PAYLOAD_V2, ErrorCode.MISSING_VERSION),
        ("", PAYLOAD_V2, ErrorCode.MISSING_VERSION),
        ("2", PAYLOAD_V2, ErrorCode.INVALID_VERSION),
        ("v2.0", PAYLOAD_V2, ErrorCode.INVALID_VERSION),
        ("v9", PAYLOAD_V2, ErrorCode.UNSUPPORTED_VERSION),
        ("v0", PAYLOAD_V2, ErrorCode.VERSION_RETIRED),
        ("v2", {"email": "e@x"}, ErrorCode.INCOMPATIBLE_VERSION),
    ],
    ids=["missing-none", "missing-empty", "invalid-no-v", "invalid-decimal",
         "unsupported", "retired", "incompatible"],
)
def test_failure_branches_identical_across_construction_styles(
    service, version, payload, expected_code
):
    conclusions = []
    for build in (legacy_request, modern_request):
        req = build("r-fail", "alice", version, "k-fail", payload)
        with pytest.raises(GatewayError) as exc_info:
            service.handle(req, handler=lambda p: pytest.fail("拒绝的请求不应进入业务"))
        conclusions.append(exc_info.value.code)
    assert conclusions == [expected_code, expected_code]


def test_retired_version_not_re_admitted_for_legacy_callers(service):
    req = legacy_request("r-ret", "alice", "v0", "k-ret", PAYLOAD_V2)
    with pytest.raises(GatewayError) as exc_info:
        service.handle(req, handler=lambda p: pytest.fail("已停用版本不应放行"))
    assert exc_info.value.code is ErrorCode.VERSION_RETIRED


def test_quota_scope_shared_across_construction_styles():
    """同一 (caller, version) 配额作用域对两种写法共同生效：
    老写法消耗掉的额度，新写法同样受限，拒绝结论与作用域一致。"""
    registry = VersionRegistry()
    registry.register(VersionRule("v2", required_fields=frozenset({"name", "email"})))
    svc = GatewayService(
        versions=registry,
        limiter=ScopedSlidingWindowRateLimiter(
            policy=QuotaPolicy(
                default_limit=100,
                caller_version_limits={("alice", "v2"): 2},
            ),
            window_seconds=60.0,
        ),
        idempotency=IdempotencyStore(),
    )
    handler = lambda p: {"echo": p}  # noqa: E731
    svc.handle(legacy_request("r1", "alice", "v2", "q1", PAYLOAD_V2), handler)
    svc.handle(modern_request("r2", "alice", "v2", "q2", PAYLOAD_V2), handler)
    # 两条写法各消耗 1 次，额度（2）已尽：第三条无论哪种写法都必须被配额拒绝
    for build in (legacy_request, modern_request):
        with pytest.raises(GatewayError) as exc_info:
            svc.handle(build("r3", "alice", "v2", "q3", PAYLOAD_V2), handler)
        err = exc_info.value
        assert err.code is ErrorCode.RATE_LIMITED
        assert err.details["scope"] == {"caller": "alice", "version": "v2"}
        assert err.details["limit"] == 2


def test_concurrent_mixed_construction_styles_converge(service):
    """并发下两种写法混发同一份请求内容：归一到同一幂等作用域，
    恰好一个执行，其余一致得到"处理中"，结论不分裂、不重复执行。"""
    import threading
    import time

    n = 40
    barrier = threading.Barrier(n)
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()
    executed = {"count": 0}

    def handler(p):
        executed["count"] += 1
        entered.set()
        release.wait(timeout=5)
        return {"echo": p}

    def run(i: int):
        build = legacy_request if i % 2 == 0 else modern_request
        req = build(f"r-{i}", "alice", "v2", "k-concurrent", PAYLOAD_V2)
        barrier.wait()
        try:
            resp = service.handle(req, handler)
            outcome = resp.status
        except GatewayError as e:
            outcome = e.code.value
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    entered.wait(timeout=5)
    time.sleep(0.05)  # 等其余线程都完成申领判定
    release.set()
    for t in threads:
        t.join()

    assert executed["count"] == 1
    assert outcomes.count("executed") == 1
    assert outcomes.count(ErrorCode.IDEMPOTENCY_IN_PROGRESS.value) == n - 1
