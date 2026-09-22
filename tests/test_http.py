"""HTTP 层：错误码到状态码的映射与 Retry-After 头。"""

from fastapi.testclient import TestClient

from gateway import SlidingWindowRateLimiter
from main import app, service


def client() -> TestClient:
    return TestClient(app)


def order(caller="alice", version="v1", key="k", payload=None):
    return {
        "caller": caller,
        "version": version,
        "idempotency_key": key,
        "payload": payload if payload is not None else {"name": "n"},
    }


def test_executed_then_replayed():
    c = client()
    service._idempotency.__init__()  # 隔离测试间状态
    r1 = c.post("/orders", json=order(key="http-k1"))
    r2 = c.post("/orders", json=order(key="http-k1"))
    assert r1.status_code == 201 and r1.json()["status"] == "executed"
    assert r2.status_code == 201 and r2.json()["status"] == "replayed"
    assert r1.json()["result"] == r2.json()["result"]


def test_version_errors_map_to_distinct_statuses():
    c = client()
    r = c.post("/orders", json=order(version="v9", key="http-k2"))
    assert r.status_code == 400
    assert r.json()["code"] == "UNSUPPORTED_VERSION"
    r = c.post("/orders", json=order(version="v2", key="http-k3"))
    assert r.status_code == 422
    assert r.json()["code"] == "INCOMPATIBLE_VERSION"


def test_rate_limit_returns_429_with_retry_after():
    c = client()
    service._limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    r1 = c.post("/orders", json=order(caller="limited", key="http-k4"))
    r2 = c.post("/orders", json=order(caller="limited", key="http-k5"))
    assert r1.status_code == 201
    assert r2.status_code == 429
    assert r2.json()["code"] == "RATE_LIMITED"
    assert float(r2.headers["Retry-After"]) > 0


def test_idempotency_conflict_is_409():
    c = client()
    service._limiter = SlidingWindowRateLimiter(limit=100, window_seconds=60.0)
    service._idempotency.__init__()
    r1 = c.post("/orders", json=order(key="http-k6", payload={"name": "a"}))
    r2 = c.post("/orders", json=order(key="http-k6", payload={"name": "b"}))
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["code"] == "IDEMPOTENCY_CONFLICT"
