"""版本生命周期：正常 / 废弃 / 停用三态，与未声明、格式非法、不认识、形状不符互可区分。"""

import datetime

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)


@pytest.fixture
def registry() -> VersionRegistry:
    reg = VersionRegistry()
    reg.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset_on=datetime.date(2027, 3, 1),
            successor="v2",
        )
    )
    reg.register(
        VersionRule("v2", required_fields=frozenset({"name", "email"})),
    )
    reg.register(VersionRule("v0", status=VersionStatus.RETIRED, successor="v2"))
    return reg


def test_active_version_served_without_notice(registry):
    registry.validate("v2", {"name": "a", "email": "e@x"})
    assert registry.deprecation_notice("v2") is None


def test_deprecated_version_still_served_and_carries_notice(registry):
    registry.validate("v1", {"name": "a"})  # 不抛异常即仍可用
    notice = registry.deprecation_notice("v1")
    assert notice is not None
    d = notice.as_dict()
    assert d["status"] == "DEPRECATED"
    assert d["sunset_on"] == "2027-03-01"
    assert d["successor"] == "v2"
    assert "v2" in d["message"] and "2027-03-01" in d["message"]


def test_retired_version_rejected_with_distinct_code(registry):
    with pytest.raises(GatewayError) as exc:
        registry.validate("v0", {})
    assert exc.value.code is ErrorCode.VERSION_RETIRED
    assert "v2" in exc.value.message  # 拒绝信息指向替代版本


def test_missing_version_is_distinct(registry):
    for bad in (None, "", "   "):
        with pytest.raises(GatewayError) as exc:
            registry.validate(bad, {})
        assert exc.value.code is ErrorCode.VERSION_MISSING


def test_malformed_version_is_distinct(registry):
    for bad in ("V1", "1", "v1.0", " v1", "v1 ", "vv1", "beta"):
        with pytest.raises(GatewayError) as exc:
            registry.validate(bad, {})
        assert exc.value.code is ErrorCode.VERSION_MALFORMED, bad


def test_unknown_version_is_unsupported(registry):
    with pytest.raises(GatewayError) as exc:
        registry.validate("v9", {})
    assert exc.value.code is ErrorCode.UNSUPPORTED_VERSION


def test_shape_mismatch_on_active_version_is_incompatible(registry):
    with pytest.raises(GatewayError) as exc:
        registry.validate("v2", {"name": "a"})
    assert exc.value.code is ErrorCode.INCOMPATIBLE_VERSION


def test_retired_rejected_before_shape_check(registry):
    """停用版本即便形状恰好匹配也要拒绝，且结论是 RETIRED 而非其他。"""
    with pytest.raises(GatewayError) as exc:
        registry.validate("v0", {"name": "a", "email": "e@x"})
    assert exc.value.code is ErrorCode.VERSION_RETIRED


def test_all_five_version_outcomes_are_distinguishable(registry):
    cases = [
        (None, {}, ErrorCode.VERSION_MISSING),
        ("V1", {}, ErrorCode.VERSION_MALFORMED),
        ("v9", {}, ErrorCode.UNSUPPORTED_VERSION),
        ("v0", {}, ErrorCode.VERSION_RETIRED),
        ("v2", {"name": "a"}, ErrorCode.INCOMPATIBLE_VERSION),
    ]
    codes = set()
    for version, payload, expected in cases:
        with pytest.raises(GatewayError) as exc:
            registry.validate(version, payload)
        assert exc.value.code is expected
        codes.add(exc.value.code)
    assert len(codes) == 5


def test_deprecated_rule_requires_sunset_and_successor():
    with pytest.raises(ValueError):
        VersionRule("v3", status=VersionStatus.DEPRECATED, sunset_on=None, successor="v4")
