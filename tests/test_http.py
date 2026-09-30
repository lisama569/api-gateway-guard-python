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


# ---- 版本生命周期 / 作用域配额 / 跨版本幂等 的 HTTP 表现 -------------------

def test_retired_version_returns_410_with_replacement():
    c = client()
    service._idempotency.__init__()
    r = c.post("/orders", json=order(version="v0", key="http-retired"))
    assert r.status_code == 410
    body = r.json()
    assert body["code"] == "VERSION_RETIRED"
    assert body["details"]["replacement"] == "v2"
    assert 'rel="successor-version"' in r.headers.get("Link", "")


def test_missing_and_invalid_version_are_distinct_and_stable():
    c = client()
    r = c.post("/orders", json={k: v for k, v in order(key="http-miss").items() if k != "version"})
    assert r.status_code == 400
    assert r.json()["code"] == "MISSING_VERSION"

    r = c.post("/orders", json=order(version="vX", key="http-invalid"))
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_VERSION"


def test_deprecated_version_response_carries_sunset_and_link():
    c = client()
    service._idempotency.__init__()
    r = c.post("/orders", json=order(version="v1", key="http-dep"))
    assert r.status_code == 201
    assert r.headers["Deprecation"] == "true"
    assert "Mon, 01 Mar 2027 00:00:00 GMT" == r.headers["Sunset"]
    assert 'rel="successor-version"' in r.headers["Link"]
    dep = r.json()["deprecation"]
    assert dep == {"status": "DEPRECATED", "sunset": "2027-03-01", "replacement": "v2"}


def test_quota_rejection_body_identifies_scope():
    c = client()
    service._idempotency.__init__()
    # 换一个全新作用域限流器，避免受全局 60 秒窗口内其他用例计数影响
    from gateway import QuotaPolicy, ScopedSlidingWindowRateLimiter

    service._limiter = ScopedSlidingWindowRateLimiter(
        policy=QuotaPolicy(
            default_limit=100,
            caller_version_limits={("migration-partner", "v1"): 5},
        ),
        window_seconds=60.0,
    )
    # migration-partner 在 v1 上额度为 5，连发 6 次
    for i in range(5):
        r = c.post("/orders", json=order(caller="migration-partner", version="v1",
                                         key=f"http-q{i}"))
        assert r.status_code == 201
    r = c.post("/orders", json=order(caller="migration-partner", version="v1",
                                     key="http-q5"))
    assert r.status_code == 429
    body = r.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["details"]["scope"] == {"caller": "migration-partner", "version": "v1"}
    assert body["details"]["matched_rule"] == "caller_version"
    assert body["details"]["remaining"] == 0
    assert float(r.headers["Retry-After"]) > 0


def test_same_idempotency_key_across_versions_does_not_replay():
    c = client()
    service._idempotency.__init__()
    r1 = c.post("/orders", json=order(version="v1", key="http-cross",
                                      payload={"name": "a"}))
    assert r1.json()["status"] == "executed"
    r2 = c.post("/orders", json=order(version="v2", key="http-cross",
                                      payload={"name": "a", "email": "e@x"}))
    assert r2.status_code == 201
    assert r2.json()["status"] == "executed"  # 没有回放 v1 的结果
    assert r2.json()["result"] != r1.json()["result"]
