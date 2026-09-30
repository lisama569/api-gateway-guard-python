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


def test_retired_version_returns_410():
    c = client()
    r = c.post("/orders", json=order(version="v0", key="http-k7", payload={}))
    assert r.status_code == 410
    assert r.json()["code"] == "VERSION_RETIRED"
    assert "v2" in r.json()["message"]


def test_missing_version_returns_400():
    c = client()
    body = {"caller": "alice", "idempotency_key": "http-k8", "payload": {"name": "n"}}
    r = c.post("/orders", json=body)
    assert r.status_code == 400
    assert r.json()["code"] == "VERSION_MISSING"


def test_malformed_version_returns_400():
    c = client()
    r = c.post("/orders", json=order(version="V1", key="http-k9"))
    assert r.status_code == 400
    assert r.json()["code"] == "VERSION_MALFORMED"


def test_deprecated_version_returns_warning():
    c = client()
    service._idempotency.__init__()
    r = c.post("/orders", json=order(version="v1", caller="depr", key="http-k10"))
    assert r.status_code == 201
    warnings = r.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["status"] == "DEPRECATED"
    assert warnings[0]["successor"] == "v2"
    assert warnings[0]["sunset_on"] == "2027-03-01"


def test_cross_version_same_key_not_replayed():
    c = client()
    service._idempotency.__init__()
    r1 = c.post("/orders", json=order(version="v1", caller="xv", key="http-k11",
                                      payload={"name": "a"}))
    r2 = c.post("/orders", json=order(version="v2", caller="xv", key="http-k11",
                                      payload={"name": "a", "email": "e@x"}))
    assert r1.status_code == 201 and r1.json()["status"] == "executed"
    assert r2.status_code == 201 and r2.json()["status"] == "executed"
    assert r2.json()["result"] != r1.json()["result"]
