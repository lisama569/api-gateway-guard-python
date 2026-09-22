"""版本治理测试：多版本共存、缺失/不支持/不兼容可区分拒绝。"""

from __future__ import annotations

import pytest

from app.errors import ErrorCode, GatewayError
from app.versioning import VersionRegistry, VersionSpec
from tests.conftest import log_request


def _registry() -> VersionRegistry:
    reg = VersionRegistry()
    # v1 只认 name；v2 额外引入 coupon_id（语义不兼容字段）
    reg.register("orders", VersionSpec("v1", lambda p: {"version": "v1", **p}, frozenset({"name"})))
    reg.register(
        "orders",
        VersionSpec("v2", lambda p: {"version": "v2", **p}, frozenset({"name", "coupon_id"})),
    )
    return reg


def test_multiple_versions_coexist() -> None:
    reg = _registry()
    log_request("version-resolve", request_id="req-v-1", resource="orders", version="v1")
    assert reg.resolve("orders", "v1", {"name": "a"})({"name": "a"})["version"] == "v1"
    log_request("version-resolve", request_id="req-v-2", resource="orders", version="v2")
    assert reg.resolve("orders", "v2", {"name": "a", "coupon_id": "c"})(
        {"name": "a", "coupon_id": "c"}
    )["version"] == "v2"


def test_missing_version_is_rejected() -> None:
    reg = _registry()
    log_request("version-missing", request_id="req-v-miss", resource="orders", version=None)
    with pytest.raises(GatewayError) as exc:
        reg.resolve("orders", None, {"name": "a"})
    assert exc.value.code is ErrorCode.VERSION_MISSING


def test_unknown_version_is_not_routed_to_latest() -> None:
    reg = _registry()
    log_request("version-unsupported", request_id="req-v-bad", resource="orders", version="v9")
    with pytest.raises(GatewayError) as exc:
        reg.resolve("orders", "v9", {"name": "a"})
    assert exc.value.code is ErrorCode.VERSION_UNSUPPORTED
    # 错误中必须能看出支持哪些版本，便于调用方纠正
    assert exc.value.extra["supported_versions"] == ("v1", "v2")


def test_old_version_rejects_new_version_field() -> None:
    reg = _registry()
    log_request(
        "version-incompatible",
        request_id="req-v-incompat",
        resource="orders",
        version="v1",
        payload={"name": "a", "coupon_id": "c"},
    )
    with pytest.raises(GatewayError) as exc:
        # v1 无法解释仅 v2 才有的 coupon_id，必须明确拒绝，不能静默接受
        reg.resolve("orders", "v1", {"name": "a", "coupon_id": "c"})
    assert exc.value.code is ErrorCode.VERSION_INCOMPATIBLE
    assert exc.value.extra["incompatible_fields"] == ["coupon_id"]


def test_error_codes_are_distinguishable() -> None:
    reg = _registry()
    cases = {
        ErrorCode.VERSION_MISSING: (None, {"name": "a"}),
        ErrorCode.VERSION_UNSUPPORTED: ("v9", {"name": "a"}),
        ErrorCode.VERSION_INCOMPATIBLE: ("v1", {"name": "a", "coupon_id": "c"}),
    }
    for code, (ver, payload) in cases.items():
        with pytest.raises(GatewayError) as exc:
            reg.resolve("orders", ver, payload)
        assert exc.value.code is code
