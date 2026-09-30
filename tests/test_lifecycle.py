"""版本生命周期：正常/废弃/停用三态，以及缺失/非法/不认识/停用/不兼容的区分。"""

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    ScopedSlidingWindowRateLimiter,
    QuotaPolicy,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayResponse, GatewayService


@pytest.fixture
def registry() -> VersionRegistry:
    reg = VersionRegistry()
    reg.register(VersionRule("v0", status=VersionStatus.RETIRED, replacement="v2"))
    reg.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset="2027-03-01",
            replacement="v2",
        )
    )
    reg.register(
        VersionRule("v2", required_fields=frozenset({"name", "email"}))
    )
    return reg


def make_service(registry: VersionRegistry) -> GatewayService:
    return GatewayService(
        versions=registry,
        limiter=ScopedSlidingWindowRateLimiter(
            policy=QuotaPolicy(default_limit=100), window_seconds=60.0
        ),
        idempotency=IdempotencyStore(),
    )


def req(version, key="k", payload=None):
    return GatewayRequest(
        request_id=f"req-{key}",
        caller="alice",
        version=version,
        idempotency_key=key,
        payload=payload if payload is not None else {"name": "a", "email": "e@x"},
    )


# ---- 三态解析 -------------------------------------------------------------

def test_active_version_resolves_without_notice(registry):
    resolved = registry.resolve("v2")
    assert resolved.status is VersionStatus.ACTIVE
    assert resolved.notice is None


def test_deprecated_version_still_served_and_carries_notice(registry):
    resolved = registry.resolve("v1")
    assert resolved.status is VersionStatus.DEPRECATED
    assert resolved.notice is not None
    assert resolved.notice.sunset == "2027-03-01"
    assert resolved.notice.replacement == "v2"

    # 废弃版本仍能正常处理请求，成功响应上带迁移公告
    svc = make_service(registry)
    resp = svc.handle(
        GatewayRequest("r1", "alice", "k1", {"name": "a"}, version="v1"),
        lambda p: {"ok": True},
    )
    assert resp.status == "executed"
    assert resp.deprecation is not None
    assert resp.deprecation.replacement == "v2"
    # 复用缓存结果的响应同样带公告
    replay = svc.handle(
        GatewayRequest("r2", "alice", "k1", {"name": "a"}, version="v1"),
        lambda p: {"ok": True},
    )
    assert replay.status == "replayed"
    assert replay.deprecation is not None
    assert replay.deprecation.sunset == "2027-03-01"


def test_retired_version_is_rejected_and_distinguishable(registry):
    with pytest.raises(GatewayError) as exc:
        registry.resolve("v0")
    assert exc.value.code is ErrorCode.VERSION_RETIRED
    assert exc.value.details["replacement"] == "v2"
    assert exc.value.details["status"] == "RETIRED"


def test_retired_differs_from_unknown_invalid_missing(registry):
    outcomes = {}
    for label, version in [
        ("missing", None),
        ("missing_blank", "   "),
        ("invalid", "vX"),
        ("invalid_number", "1"),
        ("unknown", "v9"),
        ("retired", "v0"),
    ]:
        with pytest.raises(GatewayError) as exc:
            registry.resolve(version)
        outcomes[label] = exc.value.code

    assert outcomes["missing"] is ErrorCode.MISSING_VERSION
    assert outcomes["missing_blank"] is ErrorCode.MISSING_VERSION
    assert outcomes["invalid"] is ErrorCode.INVALID_VERSION
    assert outcomes["invalid_number"] is ErrorCode.INVALID_VERSION
    assert outcomes["unknown"] is ErrorCode.UNSUPPORTED_VERSION
    assert outcomes["retired"] is ErrorCode.VERSION_RETIRED
    # 四种结论必须互不相同
    assert len({outcomes[k] for k in ("missing", "invalid", "unknown", "retired")}) == 4


def test_empty_string_version_is_missing_not_invalid(registry):
    with pytest.raises(GatewayError) as exc:
        registry.resolve("")
    assert exc.value.code is ErrorCode.MISSING_VERSION


def test_non_string_version_is_invalid(registry):
    with pytest.raises(GatewayError) as exc:
        registry.resolve(2)
    assert exc.value.code is ErrorCode.INVALID_VERSION


def test_retired_rejection_precedes_payload_check(registry):
    """已停用版本即使请求体不合法，结论仍是 VERSION_RETIRED（先看生命周期）。"""
    svc = make_service(registry)
    with pytest.raises(GatewayError) as exc:
        svc.handle(req("v0", key="x", payload={"totally": "wrong"}), lambda p: p)
    assert exc.value.code is ErrorCode.VERSION_RETIRED


def test_deprecated_version_still_validates_payload_shape(registry):
    """废弃≠放弃兼容校验：废弃版本请求体对不上仍判 INCOMPATIBLE_VERSION。"""
    with pytest.raises(GatewayError) as exc:
        registry.validate("v1", {})
    assert exc.value.code is ErrorCode.INCOMPATIBLE_VERSION


def test_incompatible_distinct_from_retired(registry):
    with pytest.raises(GatewayError) as retired:
        registry.resolve("v0")
    with pytest.raises(GatewayError) as incompatible:
        registry.validate("v2", {"name": "a"})
    assert retired.value.code is not incompatible.value.code


def test_supported_versions_excludes_retired(registry):
    assert registry.supported_versions() == ["v1", "v2"]


def test_retired_version_does_not_consume_quota_or_idempotency(registry):
    svc = make_service(registry)
    with pytest.raises(GatewayError):
        svc.handle(req("v0", key="k"), lambda p: p)
    # 停用拒绝不占用幂等键：同键在 v1 上是全新操作
    resp = svc.handle(
        GatewayRequest("r", "alice", "k", {"name": "a"}, version="v1"),
        lambda p: "fresh",
    )
    assert isinstance(resp, GatewayResponse)
    assert resp.status == "executed"


# ---- 注册期配置校验 --------------------------------------------------------

def test_deprecated_requires_sunset_and_replacement():
    reg = VersionRegistry()
    with pytest.raises(ValueError):
        reg.register(VersionRule("v1", status=VersionStatus.DEPRECATED, replacement="v2"))
    with pytest.raises(ValueError):
        reg.register(VersionRule("v1", status=VersionStatus.DEPRECATED, sunset="2027-01-01"))


def test_deprecated_sunset_must_be_iso_date():
    reg = VersionRegistry()
    with pytest.raises(ValueError):
        reg.register(
            VersionRule(
                "v1", status=VersionStatus.DEPRECATED,
                sunset="01/03/2027", replacement="v2",
            )
        )


def test_illegal_registered_version_name_rejected():
    reg = VersionRegistry()
    with pytest.raises(ValueError):
        reg.register(VersionRule("1.0"))
