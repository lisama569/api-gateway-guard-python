"""接口多版本共存、兼容性校验与版本生命周期管理。

版本生命周期：
- ACTIVE（正常提供）：正常处理请求。
- DEPRECATED（已废弃但仍可用）：正常处理请求，但响应需附带废弃提示，
  包含计划停止支持日期（sunset_on）与建议迁移的替代版本（successor）。
- RETIRED（已停止提供）：请求被明确拒绝（VERSION_RETIRED）。

五种版本相关结论互可区分：
- 请求未声明版本              -> VERSION_MISSING
- 版本值格式不合法            -> VERSION_MALFORMED
- 版本未注册（版本不认识）    -> UNSUPPORTED_VERSION
- 版本已停止提供              -> VERSION_RETIRED
- 请求体与该版本形状对不上    -> INCOMPATIBLE_VERSION

兼容规则：
- 请求体必须包含该版本声明的全部必填字段，缺失即拒绝（INCOMPATIBLE_VERSION）。
- 请求体不得携带该版本已移除的字段，携带即拒绝（INCOMPATIBLE_VERSION）。
- 旧版本请求不会被静默路由到新版本：校验只针对请求自己声明的版本进行。
"""

from __future__ import annotations

import datetime
import enum
import re
from dataclasses import dataclass, field

from gateway.errors import ErrorCode, GatewayError

# 合法版本值：v 后跟纯数字，例如 v1、v2、v12
VERSION_PATTERN = re.compile(r"^v[0-9]+$")


class VersionStatus(enum.Enum):
    """版本生命周期状态。"""

    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class VersionRule:
    """单个版本的兼容规则与生命周期声明。"""

    version: str
    required_fields: frozenset[str] = field(default_factory=frozenset)
    removed_fields: frozenset[str] = field(default_factory=frozenset)
    status: VersionStatus = VersionStatus.ACTIVE
    sunset_on: datetime.date | None = None
    successor: str | None = None

    def __post_init__(self) -> None:
        if not VERSION_PATTERN.match(self.version):
            raise ValueError(f"malformed version identifier: {self.version!r}")
        if self.status is VersionStatus.DEPRECATED:
            if self.sunset_on is None or self.successor is None:
                raise ValueError(
                    f"deprecated version {self.version!r} requires sunset_on and successor"
                )
        if self.successor is not None and not VERSION_PATTERN.match(self.successor):
            raise ValueError(f"malformed successor identifier: {self.successor!r}")


@dataclass(frozen=True)
class DeprecationNotice:
    """废弃期版本随响应返回的迁移提示。"""

    version: str
    sunset_on: str  # ISO-8601 日期
    successor: str
    message: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "status": VersionStatus.DEPRECATED.value,
            "sunset_on": self.sunset_on,
            "successor": self.successor,
            "message": self.message,
        }


class VersionRegistry:
    """版本注册表：生命周期查询与请求兼容性校验。"""

    def __init__(self) -> None:
        self._rules: dict[str, VersionRule] = {}

    def register(self, rule: VersionRule) -> None:
        if rule.version in self._rules:
            raise ValueError(f"version already registered: {rule.version}")
        self._rules[rule.version] = rule

    def supported_versions(self) -> list[str]:
        return sorted(self._rules)

    def deprecation_notice(self, version: str) -> DeprecationNotice | None:
        """版本处于废弃期时返回迁移提示；正常提供或未知版本返回 None。"""
        rule = self._rules.get(version)
        if rule is None or rule.status is not VersionStatus.DEPRECATED:
            return None
        return DeprecationNotice(
            version=rule.version,
            sunset_on=rule.sunset_on.isoformat(),
            successor=rule.successor,
            message=(
                f"version {rule.version!r} is deprecated and will be retired after "
                f"{rule.sunset_on.isoformat()}; migrate to {rule.successor!r}"
            ),
        )

    def validate(self, version: str | None, payload: dict) -> None:
        """按生命周期与形状校验请求，失败抛出带可区分错误码的 GatewayError。"""
        # 1. 未声明版本
        if version is None or not isinstance(version, str) or not version.strip():
            raise GatewayError(
                ErrorCode.VERSION_MISSING,
                "request must declare an API version",
            )
        # 2. 版本值格式不合法
        if not VERSION_PATTERN.match(version):
            raise GatewayError(
                ErrorCode.VERSION_MALFORMED,
                f"malformed version {version!r}; expected pattern 'v<number>', e.g. 'v2'",
            )
        # 3. 版本未注册（不认识）
        rule = self._rules.get(version)
        if rule is None:
            raise GatewayError(
                ErrorCode.UNSUPPORTED_VERSION,
                f"unsupported version {version!r}; supported: {self.supported_versions()}",
            )
        # 4. 版本已停止提供
        if rule.status is VersionStatus.RETIRED:
            hint = (
                f"; migrate to {rule.successor!r}" if rule.successor else ""
            )
            raise GatewayError(
                ErrorCode.VERSION_RETIRED,
                f"version {version!r} is retired and no longer served{hint}",
            )
        # 5. 请求体与该版本形状对不上
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
