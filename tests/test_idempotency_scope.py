"""跨版本/跨调用方幂等隔离：同键只在同 (caller, version) 作用域内回放或冲突。"""

import threading
import time

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyState,
    IdempotencyStore,
    QuotaPolicy,
    ScopedSlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
)
from gateway.service import GatewayRequest, GatewayResponse, GatewayService

STORE_FP_A = "fp-a"


def test_same_key_different_version_is_independent():
    store = IdempotencyStore()
    store.claim("k", STORE_FP_A, caller="alice", version="v1")
    store.complete("k", STORE_FP_A, "result-v1", caller="alice", version="v1")

    # 换版本：全新操作，不回放旧结果
    claim = store.claim("k", STORE_FP_A, caller="alice", version="v2")
    assert claim.should_execute
    assert claim.state is IdempotencyState.IN_PROGRESS
    assert claim.cached_result is None

    # v1 的缓存结果仍然可在 v1 作用域内回放
    replayed = store.claim("k", STORE_FP_A, caller="alice", version="v1")
    assert replayed.state is IdempotencyState.COMPLETED
    assert replayed.cached_result == "result-v1"


def test_same_key_different_caller_is_independent():
    store = IdempotencyStore()
    store.claim("k", STORE_FP_A, caller="alice", version="v1")
    store.complete("k", STORE_FP_A, "alice-result", caller="alice", version="v1")

    claim = store.claim("k", STORE_FP_A, caller="bob", version="v1")
    assert claim.should_execute
    store.complete("k", STORE_FP_A, "bob-result", caller="bob", version="v1")

    assert (
        store.claim("k", STORE_FP_A, caller="alice", version="v1").cached_result
        == "alice-result"
    )
    assert (
        store.claim("k", STORE_FP_A, caller="bob", version="v1").cached_result
        == "bob-result"
    )


def test_different_fingerprint_conflict_only_within_same_scope():
    """同键不同请求体只在同一作用域内算冲突；换作用域不冲突。"""
    store = IdempotencyStore()
    store.claim("k", STORE_FP_A, caller="alice", version="v1")
    # 同作用域不同指纹 -> 冲突
    with pytest.raises(GatewayError) as exc:
        store.claim("k", "fp-b", caller="alice", version="v1")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    # 换版本用不同指纹 -> 全新操作，不冲突
    claim = store.claim("k", "fp-b", caller="alice", version="v2")
    assert claim.should_execute
    # 换调用方同样不冲突
    assert store.claim("k", "fp-b", caller="bob", version="v1").should_execute


def test_failure_retry_is_scoped():
    """失败释放只影响同作用域；其他作用域不被波及。"""
    store = IdempotencyStore()
    store.claim("k", STORE_FP_A, caller="alice", version="v1")
    store.fail("k", STORE_FP_A, caller="alice", version="v1")
    # 同作用域可安全重试
    assert store.claim(
        "k", STORE_FP_A, caller="alice", version="v1"
    ).should_execute
    # v2 上本来就没有记录，也是全新申领
    assert store.claim(
        "k", STORE_FP_A, caller="alice", version="v2"
    ).should_execute


# ---- 服务端到端：跨版本/跨调用方不串结果 -----------------------------------

@pytest.fixture
def service() -> GatewayService:
    versions = VersionRegistry()
    versions.register(VersionRule("v1", required_fields=frozenset({"name"})))
    versions.register(
        VersionRule("v2", required_fields=frozenset({"name", "email"}))
    )
    return GatewayService(
        versions=versions,
        limiter=ScopedSlidingWindowRateLimiter(
            policy=QuotaPolicy(default_limit=100), window_seconds=60.0
        ),
        idempotency=IdempotencyStore(),
    )


def order(i, *, caller="alice", version="v1", key="k", payload=None):
    if payload is None:
        payload = {"name": f"n{i}"} if version == "v1" else {"name": f"n{i}", "email": "e@x"}
    return GatewayRequest(
        request_id=f"req-{i}", caller=caller, version=version,
        idempotency_key=key, payload=payload,
    )


def test_service_same_key_cross_version_executes_twice(service):
    side_effects = []

    def handler(p):
        side_effects.append(p)
        return {"handled": p["name"]}

    r1 = service.handle(order(1, version="v1", key="shared", payload={"name": "a"}), handler)
    r2 = service.handle(
        order(2, version="v2", key="shared", payload={"name": "a", "email": "e@x"}),
        handler,
    )
    assert r1.status == "executed" and r2.status == "executed"
    assert len(side_effects) == 2  # 跨版本各执行一次，不回放

    # 各自作用域内再来一次：都回放各自的结果
    r1b = service.handle(order(3, version="v1", key="shared", payload={"name": "a"}), handler)
    r2b = service.handle(
        order(4, version="v2", key="shared", payload={"name": "a", "email": "e@x"}),
        handler,
    )
    assert r1b.status == "replayed" and r1b.result == {"handled": "a"}
    assert r2b.status == "replayed" and r2b.result == {"handled": "a"}
    assert len(side_effects) == 2  # 回放均不触发副作用


def test_service_same_key_cross_caller_executes_twice(service):
    calls = []
    r1 = service.handle(order(1, caller="alice", key="shared", payload={"name": "a"}),
                        lambda p: calls.append(1) or {"who": "alice"})
    r2 = service.handle(order(2, caller="bob", key="shared", payload={"name": "a"}),
                        lambda p: calls.append(1) or {"who": "bob"})
    assert r1.status == r2.status == "executed"
    assert r1.result != r2.result
    assert len(calls) == 2


def test_service_cross_version_same_key_same_body_is_not_conflict(service):
    """跨版本同键即便请求体不同也绝不能误判成冲突。"""
    service.handle(order(1, version="v1", key="k", payload={"name": "a"}), lambda p: p)
    # 不同版本、不同体：应正常执行而不是 IDEMPOTENCY_CONFLICT
    resp = service.handle(
        order(2, version="v2", key="k", payload={"name": "b", "email": "z@z"}),
        lambda p: p,
    )
    assert resp.status == "executed"


def test_service_in_progress_is_scoped_under_concurrency(service):
    """同一键处理中：同作用域得到 IN_PROGRESS；另一作用域并发请求正常执行。"""
    release = threading.Event()
    handler_entered = threading.Event()
    executed = []
    results = []
    barrier = threading.Barrier(4)

    def slow_handler(p):
        executed.append(1)
        handler_entered.set()
        release.wait(timeout=5)
        return {"echo": p}

    # 每个作用域使用各自合法且互不相同的请求体，避免与作用域隔离无关的干扰。
    payload_by_scope = {
        ("alice", "v1"): {"name": "alice-v1"},
        ("alice", "v2"): {"name": "alice-v2", "email": "a@x"},
        ("bob", "v1"): {"name": "bob-v1"},
    }

    def run(desc, caller, version):
        barrier.wait()
        try:
            results.append(
                (desc, service.handle(order(0, caller=caller, version=version,
                                            key="hot",
                                            payload=payload_by_scope[(caller, version)]),
                                      slow_handler))
            )
        except GatewayError as e:
            results.append((desc, e))

    threads = [
        threading.Thread(target=run, args=("alice-v1-a", "alice", "v1")),
        threading.Thread(target=run, args=("alice-v1-b", "alice", "v1")),
        threading.Thread(target=run, args=("alice-v2", "alice", "v2")),
        threading.Thread(target=run, args=("bob-v1", "bob", "v1")),
    ]
    for t in threads:
        t.start()
    # 等首个拿到执行权的处理进入处理中，保证其余同作用域请求确定撞上 IN_PROGRESS
    handler_entered.wait(timeout=5)
    # 再给其余线程一点时间完成各自的申领判定
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join()

    by_desc = dict(results)
    # alice/v1 两条：恰好一个执行、一个处理中
    a1 = [v for k, v in results if k.startswith("alice-v1")]
    assert sum(isinstance(v, GatewayResponse) for v in a1) == 1
    assert any(
        isinstance(v, GatewayError) and v.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
        for v in a1
    )
    # 换版本、换调用方都正常执行，不被处理中状态阻塞
    assert isinstance(by_desc["alice-v2"], GatewayResponse)
    assert isinstance(by_desc["bob-v1"], GatewayResponse)
    assert len(executed) == 3


def test_concurrent_same_key_across_three_scopes_each_executes_once(service):
    """同键在 (alice,v1)/(alice,v2)/(bob,v1) 三个作用域各并发 20 次：
    每个作用域恰好 1 次执行，其余处理中，绝不串结果。"""
    entered = threading.Event()
    release = threading.Event()
    executed: dict[tuple, int] = {}
    results: list[tuple[tuple, object]] = []
    lock = threading.Lock()
    n_per_scope = 20
    barrier = threading.Barrier(n_per_scope * 3)

    scopes = [("alice", "v1", {"name": "a1"}),
              ("alice", "v2", {"name": "a2", "email": "e@x"}),
              ("bob", "v1", {"name": "b1"})]

    def handler(p):
        key = (p["name"],)
        with lock:
            executed[key] = executed.get(key, 0) + 1
        entered.set()
        release.wait(timeout=5)
        return {"from": p["name"]}

    def run(caller, version, payload):
        barrier.wait()
        scope = (caller, version)
        try:
            resp = service.handle(
                GatewayRequest("r", caller, "same-key", payload, version=version),
                handler,
            )
            with lock:
                results.append((scope, resp))
        except GatewayError as e:
            with lock:
                results.append((scope, e))

    threads = []
    for caller, version, payload in scopes:
        for _ in range(n_per_scope):
            threads.append(threading.Thread(target=run, args=(caller, version, payload)))
    for t in threads:
        t.start()
    entered.wait(timeout=5)
    time.sleep(0.05)  # 等其余请求在各作用域内完成申领判定
    release.set()
    for t in threads:
        t.join()

    # 每个作用域恰好 1 次副作用
    assert executed == {("a1",): 1, ("a2",): 1, ("b1",): 1}
    for caller, version, payload in scopes:
        scope = (caller, version)
        scoped = [v for s, v in results if s == scope]
        responses = [v for v in scoped if isinstance(v, GatewayResponse)]
        in_progress = [
            v for v in scoped
            if isinstance(v, GatewayError) and v.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
        ]
        assert len(responses) == 1
        assert len(in_progress) == n_per_scope - 1
        assert responses[0].result == {"from": payload["name"]}
