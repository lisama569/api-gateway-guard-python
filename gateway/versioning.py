"""接口多版本共存与兼容性校验。

规则：
- 请求必须显式声明版本；未注册的版本直接拒绝（UNSUPPORTED_VERSION）。
- 请求体必须包含该版本声明的全部必填字段，缺失即拒绝（INCOMPATIBLE_VERSION）。
- 请求体不得携带该版本已移除的字段，携带即拒绝（INCOMPATIBLE_VERSION）。
- 旧版本请求不会被静默路由到新版本：校验只针对请求自己声明的版本进行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gateway.errors import ErrorCode, GatewayError


@dataclass(frozen=True)
class VersionRule:
    """单个版本的兼容规则声明。"""

    version: str
    required_fields: frozenset[str] = field(default_factory=frozenset)
    removed_fields: frozenset[str] = field(default_factory=frozenset)


class VersionRegistry:
    """版本注册表：校验请求是否匹配其声明版本。"""

    def __init__(self) -> None:
        self._rules: dict[str, VersionRule] = {}

    def register(self, rule: VersionRule) -> None:
        if rule.version in self._rules:
            raise ValueError(f"version already registered: {rule.version}")
        self._rules[rule.version] = rule

    def supported_versions(self) -> list[str]:
        return sorted(self._rules)

    def validate(self, version: str, payload: dict) -> None:
        """校验请求体与声明版本的兼容性，失败抛出 GatewayError。"""
        rule = self._rules.get(version)
        if rule is None:
            raise GatewayError(
                ErrorCode.UNSUPPORTED_VERSION,
                f"unsupported version {version!r}; supported: {self.supported_versions()}",
            )
        missing = sorted(rule.required_fields - payload.keys())
        if missing:
            raise GatewayError(
                ErrorCode.INCOMPATIBLE_VERSION,
                f"version {version!r} requires fields {missing}",
            )
        removed = sorted(rule.removed_fields & payload.keys())
        if removed:
            raise GatewayError(
                ErrorCode.INCOMPATIBLE_VERSION,
                f"version {version!r} removed fields {removed}; do not send them",
            )
