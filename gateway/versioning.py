"""接口版本生命周期管理与兼容性校验。

版本三种状态：
- ACTIVE（正常提供）
- DEPRECATED（已废弃但仍可用）：请求正常处理，响应附带废弃公告
  （Sunset 日期与替代版本）。
- RETIRED（已停止提供）：请求被明确拒绝（VERSION_RETIRED）。

版本相关失败互可区分：
- MISSING_VERSION    ：请求未声明版本。
- INVALID_VERSION    ：版本值格式不合法（当前要求形如 v1、v2 的 v<数字>）。
- UNSUPPORTED_VERSION：格式合法，但注册表里没有这个版本（"版本不认识"）。
- VERSION_RETIRED    ：版本注册过且曾提供，现已停止。
- INCOMPATIBLE_VERSION：版本可用，但请求体与该版本声明对不上。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from gateway.errors import ErrorCode, GatewayError

_VERSION_FORMAT = "v<非负整数>，例如 v1、v2"
_VERSION_RE = re.compile(r"v(?:0|[1-9]\d*)")


class VersionStatus(enum.Enum):
    """版本生命周期状态。"""

    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class VersionRule:
    """单个版本的生命周期状态与兼容规则声明。"""

    version: str
    required_fields: frozenset[str] = field(default_factory=frozenset)
    removed_fields: frozenset[str] = field(default_factory=frozenset)
    status: VersionStatus = VersionStatus.ACTIVE
    #: 废弃版本计划停止支持的日期（ISO 格式 YYYY-MM-DD），DEPRECATED 必填。
    sunset: str | None = None
    #: 建议调用方迁移到的替代版本号，DEPRECATED 必填。
    replacement: str | None = None


@dataclass(frozen=True)
class DeprecationNotice:
    """废弃期版本随每个成功响应返回的迁移公告。"""

    version: str
    sunset: str
    replacement: str


@dataclass(frozen=True)
class ResolvedVersion:
    """版本解析结果：状态 + （若处于废弃期）迁移公告。"""

    version: str
    status: VersionStatus
    notice: DeprecationNotice | None


class VersionRegistry:
    """版本注册表：解析生命周期状态并校验请求是否匹配其声明版本。"""

    def __init__(self) -> None:
        self._rules: dict[str, VersionRule] = {}

    def register(self, rule: VersionRule) -> None:
        """注册一个版本；配置不合法（重复注册/格式错误/缺公告字段）抛 ValueError。"""
        if not isinstance(rule.version, str) or not _VERSION_RE.fullmatch(rule.version):
            raise ValueError(f"illegal version name {rule.version!r}; expected {_VERSION_FORMAT}")
        if rule.version in self._rules:
            raise ValueError(f"version already registered: {rule.version}")
        if rule.status is VersionStatus.DEPRECATED:
            if not rule.sunset or not rule.replacement:
                raise ValueError(
                    f"deprecated version {rule.version!r} requires sunset and replacement"
                )
            date.fromisoformat(rule.sunset)  # 校验 ISO 日期，非法时抛 ValueError
            if rule.replacement == rule.version:
                raise ValueError(f"deprecated version {rule.version!r} cannot replace itself")
        self._rules[rule.version] = rule

    def supported_versions(self) -> list[str]:
        """当前仍可提供服务（ACTIVE/DEPRECATED）的版本，按版本号排序。"""
        return sorted(
            (v for v, r in self._rules.items() if r.status is not VersionStatus.RETIRED),
            key=lambda v: int(v[1:]),
        )

    def resolve(self, version: Any) -> ResolvedVersion:
        """解析请求声明的版本：未声明/格式非法/不认识/已停用分别抛对应错误码。"""
        # 1. 未声明：None / 空串（去除空白后）都算未声明，与"格式不合法"分开。
        if version is None or (isinstance(version, str) and not version.strip()):
            raise GatewayError(
                ErrorCode.MISSING_VERSION,
                f"request did not declare a version; expected {_VERSION_FORMAT}",
            )
        # 2. 类型不对或格式不合法：稳定结论 INVALID_VERSION，不能落到"不认识"上。
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise GatewayError(
                ErrorCode.INVALID_VERSION,
                f"invalid version {version!r}; expected {_VERSION_FORMAT}",
            )
        rule = self._rules.get(version)
        # 3. 格式合法但注册表里没有：版本不认识。
        if rule is None:
            raise GatewayError(
                ErrorCode.UNSUPPORTED_VERSION,
                f"unsupported version {version!r}; supported: {self.supported_versions()}",
                details={"supported": self.supported_versions()},
            )
        # 4. 注册过但已停止提供：与"不认识"明确区分，并指引替代版本。
        if rule.status is VersionStatus.RETIRED:
            details = {"status": VersionStatus.RETIRED.value}
            if rule.replacement:
                details["replacement"] = rule.replacement
            raise GatewayError(
                ErrorCode.VERSION_RETIRED,
                f"version {version!r} is retired and no longer served"
                + (f"; migrate to {rule.replacement!r}" if rule.replacement else ""),
                details=details,
            )
        notice = None
        if rule.status is VersionStatus.DEPRECATED:
            notice = DeprecationNotice(
                version=rule.version,
                sunset=rule.sunset,  # type: ignore[arg-type]
                replacement=rule.replacement,  # type: ignore[arg-type]
            )
        return ResolvedVersion(version=rule.version, status=rule.status, notice=notice)

    def check_payload(self, version: str, payload: Mapping[str, Any]) -> None:
        """校验请求体与一个已解析版本的兼容性，失败抛 INCOMPATIBLE_VERSION。"""
        rule = self._rules.get(version)
        if rule is None:  # 未先 resolve 属于编程错误
            raise ValueError(f"version not registered: {version!r}")
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

    def validate(self, version: Any, payload: Mapping[str, Any]) -> None:
        """resolve + check_payload，兼容旧调用方。"""
        resolved = self.resolve(version)
        self.check_payload(resolved.version, payload)
