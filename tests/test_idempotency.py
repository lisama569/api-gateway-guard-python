"""幂等测试：副作用只触发一次、状态可区分、失败可安全重试。"""

from __future__ import annotations

import threading

import pytest

from app.errors import ErrorCode, GatewayError
from app.idempotency import IdempotencyStatus, IdempotencyStore, canonical_fingerprint
from tests.conftest import log_request


def make_handler():
    state = {"calls": 0}

    def handler(payload):
        state["calls"] += 1
        return {"received": payload}

    return handler, state


def test_first_completes_second_replays_without_side_effect() -> None:
    store = IdempotencyStore()
    handler, state = make_handler()

    o1 = store.execute("key-1", {"a": 1}, handler, "req-idem-1")
    log_request("idempotency-first", request_id="req-idem-1", key="key-1", status=o1.status.value)
    assert o1.status is IdempotencyStatus.COMPLETED

    o2 = store.execute("key-1", {"a": 1}, handler, "req-idem-2")
    log_request("idempotency-replay", request_id="req-idem-2", key="key-1", status=o2.status.value)
    assert o2.status is IdempotencyStatus.REPLAYED
    assert o2.result == o1.result
    # 关键：重复请求不得重复触发副作用
    assert state["calls"] == 1


def test_same_key_different_payload_is_conflict() -> None:
    store = IdempotencyStore()
    handler, state = make_handler()
    store.execute("key-c", {"a": 1}, handler, "req-idem-c1")

    log_request("idempotency-conflict", request_id="req-idem-c2", key="key-c")
    outcome = store.execute("key-c", {"a": 2}, handler, "req-idem-c2")
    assert outcome.status is IdempotencyStatus.CONFLICT
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert state["calls"] == 1


def test_processing_duplicate_is_distinguishable() -> None:
    store = IdempotencyStore()
    started = threading.Event()
    release = threading.Event()

    def slow_handler(payload):
        started.set()
        release.wait(timeout=2)
        return {"done": True}

    t = threading.Thread(target=store.execute, args=("key-p", {"x": 1}, slow_handler, "req-idem-p1"))
    t.start()
    assert started.wait(timeout=2)

    log_request("idempotency-processing", request_id="req-idem-p2", key="key-p")
    outcome = store.execute("key-p", {"x": 1}, slow_handler, "req-idem-p2")
    assert outcome.status is IdempotencyStatus.PROCESSING
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.IDEMPOTENCY_PROCESSING
    # 调用方可从错误信息拿到重试建议
    assert outcome.error.extra["retry_after_seconds"] >= 1

    release.set()
    t.join()
    # 完成后再请求即复用结果
    after = store.execute("key-p", {"x": 1}, slow_handler, "req-idem-p3")
    assert after.status is IdempotencyStatus.REPLAYED
    assert after.result == {"done": True}


def test_failure_can_be_safely_retried() -> None:
    store = IdempotencyStore()
    attempts = {"n": 0}

    def flaky(payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("downstream boom")
        return {"ok": True}

    log_request("idempotency-fail", request_id="req-idem-r1", key="key-r")
    with pytest.raises(RuntimeError):
        store.execute("key-r", {"q": 1}, flaky, "req-idem-r1")

    log_request("idempotency-retry", request_id="req-idem-r2", key="key-r")
    outcome = store.execute("key-r", {"q": 1}, flaky, "req-idem-r2")
    assert outcome.status is IdempotencyStatus.RETRIED
    assert outcome.result == {"ok": True}

    # 再次重复只复用，不再执行
    again = store.execute("key-r", {"q": 1}, flaky, "req-idem-r3")
    assert again.status is IdempotencyStatus.REPLAYED
    assert attempts["n"] == 2


def test_concurrent_same_key_triggers_side_effect_once() -> None:
    store = IdempotencyStore()
    barrier = threading.Barrier(8)
    calls = {"n": 0}
    lock = threading.Lock()

    def handler(payload):
        with lock:
            calls["n"] += 1
        return {"v": calls["n"]}

    results: list = []

    def worker() -> None:
        barrier.wait()
        results.append(store.execute("key-z", {"z": 1}, handler, "req-idem-conc"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_request(
        "idempotency-concurrent",
        request_id="req-idem-conc",
        key="key-z",
        statuses=sorted(r.status.value for r in results),
    )
    # 恰好一个 COMPLETED，其余全部 REPLAYED 或 PROCESSING，无 CONFLICT
    statuses = [r.status for r in results]
    assert statuses.count(IdempotencyStatus.COMPLETED) == 1
    assert all(s in (IdempotencyStatus.COMPLETED, IdempotencyStatus.REPLAYED,
                     IdempotencyStatus.PROCESSING) for s in statuses)
    assert calls["n"] == 1


def test_fingerprint_is_order_insensitive() -> None:
    assert canonical_fingerprint({"a": 1, "b": 2}) == canonical_fingerprint({"b": 2, "a": 1})
    assert canonical_fingerprint({"a": 1}) != canonical_fingerprint({"a": 2})
