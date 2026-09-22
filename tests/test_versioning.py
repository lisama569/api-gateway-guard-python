"""版本兼容性：不兼容请求必须被明确拒绝且错误可区分。"""

import pytest

from gateway import ErrorCode, GatewayError, VersionRegistry, VersionRule


@pytest.fixture
def registry() -> VersionRegistry:
    reg = VersionRegistry()
    reg.register(VersionRule("v1", required_fields=frozenset({"name"})))
    reg.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    return reg


def test_each_version_accepts_its_own_shape(registry):
    registry.validate("v1", {"name": "a"})
    registry.validate("v2", {"name": "a", "email": "x@y.z"})


def test_unregistered_version_rejected(registry):
    with pytest.raises(GatewayError) as exc:
        registry.validate("v9", {"name": "a"})
    assert exc.value.code is ErrorCode.UNSUPPORTED_VERSION


def test_v1_request_not_silently_upgraded_to_v2(registry):
    """v1 形状的请求声明 v2 时必须被拒绝，而不是被当作 v1 接受。"""
    with pytest.raises(GatewayError) as exc:
        registry.validate("v2", {"name": "a"})
    assert exc.value.code is ErrorCode.INCOMPATIBLE_VERSION
    assert "email" in exc.value.message


def test_removed_field_rejected(registry):
    with pytest.raises(GatewayError) as exc:
        registry.validate("v2", {"name": "a", "email": "x@y.z", "legacy_id": 7})
    assert exc.value.code is ErrorCode.INCOMPATIBLE_VERSION
    assert "legacy_id" in exc.value.message


def test_error_codes_are_distinguishable(registry):
    codes = set()
    for version, payload in [("v9", {}), ("v2", {"name": "a"})]:
        with pytest.raises(GatewayError) as exc:
            registry.validate(version, payload)
        codes.add(exc.value.code)
    assert codes == {ErrorCode.UNSUPPORTED_VERSION, ErrorCode.INCOMPATIBLE_VERSION}
