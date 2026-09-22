"""幂等：重复请求不重复触发副作用，冲突/处理中/已完成可区分，失败可安全重试。"""

import threading

import pytest

from gateway import ErrorCode, GatewayError, IdempotencyState, IdempotencyStore


def test_first_claim_executes():
    store = IdempotencyStore()
    claim = store.claim("k", "fp")
    assert claim.should_execute
    assert claim.state is IdempotencyState.IN_PROGRESS


def test_duplicate_while_in_progress_does_not_execute():
    store = IdempotencyStore()
    store.claim("k", "fp")
    claim = store.claim("k", "fp")
    assert not claim.should_execute
    assert claim.state is IdempotencyState.IN_PROGRESS


def test_completed_replay_returns_cached_result():
    store = IdempotencyStore()
    store.claim("k", "fp")
    store.complete("k", "fp", {"order_id": "o-1"})
    claim = store.claim("k", "fp")
    assert not claim.should_execute
    assert claim.state is IdempotencyState.COMPLETED
    assert claim.cached_result == {"order_id": "o-1"}


def test_same_key_different_fingerprint_is_conflict():
    store = IdempotencyStore()
    store.claim("k", "fp-a")
    with pytest.raises(GatewayError) as exc:
        store.claim("k", "fp-b")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_failure_allows_safe_retry():
    store = IdempotencyStore()
    store.claim("k", "fp")
    store.fail("k", "fp")
    assert store.state_of("k") is IdempotencyState.FAILED
    claim = store.claim("k", "fp")
    assert claim.should_execute
    store.complete("k", "fp", "ok")
    assert store.claim("k", "fp").cached_result == "ok"


def test_concurrent_claims_trigger_exactly_one_execution():
    """50 个线程同时申领同一幂等键：只有 1 个获得执行权。"""
    store = IdempotencyStore()
    claims = []
    barrier = threading.Barrier(50)

    def claim():
        barrier.wait()
        claims.append(store.claim("k", "fp"))

    threads = [threading.Thread(target=claim) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executors = [c for c in claims if c.should_execute]
    waiters = [c for c in claims if not c.should_execute]
    assert len(executors) == 1
    assert len(waiters) == 49
    assert all(c.state is IdempotencyState.IN_PROGRESS for c in waiters)


def test_states_are_distinguishable():
    store = IdempotencyStore()
    store.claim("k1", "fp")
    in_progress = store.claim("k1", "fp")
    store.complete("k1", "fp", 1)
    completed = store.claim("k1", "fp")
    conflict_code = None
    try:
        store.claim("k1", "other")
    except GatewayError as e:
        conflict_code = e.code
    assert in_progress.state is IdempotencyState.IN_PROGRESS
    assert completed.state is IdempotencyState.COMPLETED
    assert conflict_code is ErrorCode.IDEMPOTENCY_CONFLICT
