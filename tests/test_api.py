"""HTTP 端到端：版本不兼容、并发超限、重复幂等的结论一致且可区分。"""

from __future__ import annotations

import concurrent.futures as cf

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


@pytest.fixture()
def client() -> TestClient:
    # 小窗口配额，便于在测试中稳定触发限流
    return TestClient(create_app(rate_limit=5, window_seconds=60))


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_versions_coexist_and_incompatible_rejected(client: TestClient) -> None:
    # v1 正常
    r1 = client.post(
        "/api/orders",
        headers={"X-Caller": "c-ver", "X-Api-Version": "v1", "X-Request-Id": "req-http-v1"},
        json={"name": "book"},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["api_version"] == "v1"

    # v2 正常（v2 特有 coupon_id）
    r2 = client.post(
        "/api/orders",
        headers={"X-Caller": "c-ver2", "X-Api-Version": "v2", "Idempotency-Key": "k-v2",
                 "X-Request-Id": "req-http-v2"},
        json={"name": "book", "coupon_id": "CP1"},
    )
    assert r2.status_code == 201
    assert r2.json()["data"]["discount"] == "10%"

    # v1 携带 v2 字段：必须被明确拒绝，不能静默接受
    bad = client.post(
        "/api/orders",
        headers={"X-Caller": "c-ver3", "X-Api-Version": "v1", "X-Request-Id": "req-http-bad"},
        json={"name": "book", "coupon_id": "CP1"},
    )
    assert bad.status_code == 422
    body = bad.json()["error"]
    assert body["code"] == "VERSION_INCOMPATIBLE"
    assert body["incompatible_fields"] == ["coupon_id"]


@pytest.mark.parametrize(
    "headers,expected_code,expected_status",
    [
        ({"X-Api-Version": "v9"}, "VERSION_UNSUPPORTED", 404),
        ({}, "VERSION_MISSING", 400),
    ],
)
def test_version_errors_distinguishable(
    client: TestClient, headers: dict[str, str], expected_code: str, expected_status: int
) -> None:
    h = {"X-Caller": "c-x", "X-Request-Id": "req-http-verr", **headers}
    resp = client.post("/api/orders", headers=h, json={"name": "a"})
    assert resp.status_code == expected_status
    assert resp.json()["error"]["code"] == expected_code


def test_concurrent_rate_limit_is_accurate(client: TestClient) -> None:
    def call(_: int):
        return client.post(
            "/api/orders",
            headers={"X-Caller": "rate-burst", "X-Api-Version": "v1", "X-Request-Id": "req-http-rl"},
            json={"name": "x"},
        )

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(call, range(20)))

    codes = sorted(r.status_code for r in responses)
    success = [s for s in codes if 200 <= s < 300]
    assert len(success) == 5
    assert codes.count(429) == 15
    rejected = [r for r in responses if r.status_code == 429]
    for r in rejected:
        err = r.json()["error"]
        assert err["code"] == "RATE_LIMITED"
        assert err["reason"] == "QUOTA_EXCEEDED"
        assert err["retry_after"] >= 1
        # Retry-After 头可用于判断重试时机
        assert int(r.headers["Retry-After"]) >= 1


def test_idempotent_replay_returns_cached_result(client: TestClient) -> None:
    headers = {
        "X-Caller": "c-idem",
        "X-Api-Version": "v1",
        "Idempotency-Key": "idem-123",
        "X-Request-Id": "req-http-idem",
    }
    first = client.post("/api/orders", headers=headers, json={"name": "pen"})
    second = client.post("/api/orders", headers=headers, json={"name": "pen"})
    assert first.status_code == 201
    assert first.headers["Idempotency-Status"] == "COMPLETED"
    assert second.status_code == 200
    assert second.headers["Idempotency-Status"] == "REPLAYED"
    # 复用同一下单结果：order_id 相同（副作用只发生一次）
    assert first.json()["data"]["order_id"] == second.json()["data"]["order_id"]

    # 相同 key 不同 body：冲突
    conflict = client.post(
        "/api/orders", headers=headers, json={"name": "pen", "note": "changed"}
    )
    # note 字段在 v1/v2 均不被接受 -> 先被版本校验拦截（VERSION_INCOMPATIBLE）
    assert conflict.json()["error"]["code"] in ("IDEMPOTENCY_CONFLICT", "VERSION_INCOMPATIBLE")


def test_idempotency_conflict_directly(client: TestClient) -> None:
    base = {
        "X-Caller": "c-idem2",
        "X-Api-Version": "v2",
        "Idempotency-Key": "idem-999",
        "X-Request-Id": "req-http-conflict",
    }
    r1 = client.post("/api/orders", headers=base, json={"name": "a", "coupon_id": "C1"})
    assert r1.status_code == 201
    r2 = client.post("/api/orders", headers=base, json={"name": "a", "coupon_id": "C2"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
