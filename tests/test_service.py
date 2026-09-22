"""端到端：并发超限、重复幂等、版本不兼容的结论一致且原因可区分。

日志断言：单测日志中必须出现请求标识、限流判定与幂等状态。
"""

import logging
import threading

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    SlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
)
from gateway.service import GatewayRequest, GatewayResponse, GatewayService


@pytest.fixture
def service() -> GatewayService:
    versions = VersionRegistry()
    versions.register(VersionRule("v1", required_fields=frozenset({"name"})))
    versions.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    return GatewayService(
        versions=versions,
        limiter=SlidingWindowRateLimiter(limit=5, window_seconds=60.0),
        idempotency=IdempotencyStore(),
    )


def make_request(i: int, *, caller="alice", version="v1", key=None, payload=None) -> GatewayRequest:
    return GatewayRequest(
        request_id=f"req-{i}",
        caller=caller,
        version=version,
        idempotency_key=key or f"idem-{i}",
        payload=payload if payload is not None else {"name": f"n{i}"},
    )


def test_concurrent_over_limit_rejections_are_consistent(service, caplog):
    """20 个并发请求争抢 5 个配额：恰好 5 个放行、15 个带 retry_after 拒绝。"""
    outcomes = []
    barrier = threading.Barrier(20)
    with caplog.at_level(logging.INFO, logger="gateway.service"):
        def run(i):
            barrier.wait()
            try:
                outcomes.append(("ok", service.handle(make_request(i), lambda p: p)))
            except GatewayError as e:
                outcomes.append((e.code, e))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    ok = [o for o in outcomes if o[0] == "ok"]
    limited = [o for o in outcomes if o[0] is ErrorCode.RATE_LIMITED]
    assert len(ok) == 5
    assert len(limited) == 15
    assert all(e.retry_after is not None and e.retry_after > 0 for _, e in limited)
    # 日志包含请求标识与限流判定
    assert "rate_limit" in caplog.text
    assert "allowed=False" in caplog.text
    assert any(f"req-{i}" in caplog.text for i in range(20))


class _CountingStore(IdempotencyStore):
    """统计申领次数，用于让测试中的业务处理等到所有并发申领完成。"""

    def __init__(self, expected: int) -> None:
        super().__init__()
        self._expected = expected
        self._count = 0
        self._count_lock = threading.Lock()
        self.all_claimed = threading.Event()

    def claim(self, key, fingerprint):
        claim = super().claim(key, fingerprint)
        with self._count_lock:
            self._count += 1
            if self._count >= self._expected:
                self.all_claimed.set()
        return claim


def test_duplicate_idempotency_under_concurrency(service, caplog):
    """同一幂等键并发重复提交：副作用只发生一次，其余得到处理中拒绝。"""
    store = _CountingStore(expected=10)
    service._idempotency = store
    service._limiter = SlidingWindowRateLimiter(limit=100, window_seconds=60.0)
    side_effects = []
    results = []
    barrier = threading.Barrier(10)

    def handler(p):
        # 等所有并发申领完成后再返回，保证其余请求确定落在 IN_PROGRESS 上
        store.all_claimed.wait(timeout=5)
        side_effects.append(1)
        return {"created": p["name"]}

    with caplog.at_level(logging.INFO, logger="gateway.service"):
        def run(i):
            barrier.wait()
            req = make_request(i, key="same-key", payload={"name": "fixed"})
            try:
                results.append(service.handle(req, handler))
            except GatewayError as e:
                results.append(e)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(side_effects) == 1  # 副作用只触发一次
    executed = [r for r in results if isinstance(r, GatewayResponse) and r.status == "executed"]
    in_progress = [r for r in results if isinstance(r, GatewayError) and r.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS]
    assert len(executed) == 1
    assert len(in_progress) == 9
    # 处理完成后再来同一键：复用缓存结果，不再执行
    replay = service.handle(make_request(99, key="same-key", payload={"name": "fixed"}), handler)
    assert replay.status == "replayed"
    assert replay.result == {"created": "fixed"}
    assert len(side_effects) == 1
    # 日志包含幂等状态
    assert "idempotency_key=same-key" in caplog.text
    assert "state=IN_PROGRESS" in caplog.text


def test_idempotency_conflict_distinguishable(service):
    service.handle(make_request(1, key="k", payload={"name": "a"}), lambda p: p)
    with pytest.raises(GatewayError) as exc:
        service.handle(make_request(2, key="k", payload={"name": "different"}), lambda p: p)
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_failed_handler_releases_key_for_retry(service):
    payload = {"name": "retry-me"}

    def boom(_):
        raise RuntimeError("downstream exploded")

    with pytest.raises(RuntimeError):
        service.handle(make_request(1, key="k", payload=payload), boom)
    # 失败后可安全重试同一幂等键（相同请求体）
    resp = service.handle(make_request(2, key="k", payload=payload), lambda p: {"recovered": True})
    assert resp.status == "executed"
    assert resp.result == {"recovered": True}


def test_incompatible_versions_rejected_with_distinct_codes(service):
    with pytest.raises(GatewayError) as unsupported:
        service.handle(make_request(1, version="v9"), lambda p: p)
    assert unsupported.value.code is ErrorCode.UNSUPPORTED_VERSION

    with pytest.raises(GatewayError) as missing:
        service.handle(make_request(2, version="v2", payload={"name": "a"}), lambda p: p)
    assert missing.value.code is ErrorCode.INCOMPATIBLE_VERSION

    with pytest.raises(GatewayError) as removed:
        service.handle(
            make_request(3, version="v2", payload={"name": "a", "email": "e@x", "legacy_id": 1}),
            lambda p: p,
        )
    assert removed.value.code is ErrorCode.INCOMPATIBLE_VERSION
    assert unsupported.value.code is not missing.value.code


def test_rejection_does_not_consume_idempotency_or_quota(service):
    """版本校验失败的请求不得消耗限流配额，也不得占用幂等键。"""
    with pytest.raises(GatewayError):
        service.handle(make_request(1, version="v9", key="k"), lambda p: p)
    resp = service.handle(make_request(2, key="k"), lambda p: "fine")
    assert resp.status == "executed"
