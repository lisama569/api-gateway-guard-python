"""多版本接口治理：版本协商与不兼容请求拒绝。

治理规则（明确、可解释）：

1. 请求必须显式携带版本标识（``X-Api-Version`` 头或 payload 中的
   ``api_version`` 字段），缺失一律拒绝，绝不猜测/静默降级。
2. 版本必须在该资源的注册表中存在，不存在则拒绝，不会被静默路由到
   最新版本。
3. 请求体只允许携带目标版本声明接受的字段；若携带了仅属于其他版本、
   目标版本无法解释的字段，按"版本不兼容"拒绝，避免旧版本语义被
   新版本字段静默污染。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.errors import ErrorCode, GatewayError

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class VersionSpec:
    """一个版本的注册描述。"""

    version: str
    handler: Handler
    # 该版本接受并解释的请求字段集合；用于跨版本语义不兼容检测
    accepted_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass
class VersionRegistry:
    """资源 -> 版本集合的注册表。"""

    _routes: dict[str, dict[str, VersionSpec]] = field(default_factory=dict)

    def register(self, resource: str, spec: VersionSpec) -> None:
        """注册某资源的一个版本；同资源同版本重复注册将被拒绝。"""
        versions = self._routes.setdefault(resource, {})
        if spec.version in versions:
            raise ValueError(f"版本已注册: resource={resource} version={spec.version}")
        versions[spec.version] = spec

    def supported_versions(self, resource: str) -> tuple[str, ...]:
        return tuple(sorted(self._routes.get(resource, {})))

    def _reject(self, code: ErrorCode, message: str, **extra: Any) -> None:
        raise GatewayError(code, message, **extra)

    def resolve(self, resource: str, version: str | None, payload: dict[str, Any]) -> Handler:
        """按版本解析处理器。

        失败时抛出三类可区分错误：

        * ``VERSION_MISSING``    —— 未携带版本标识；
        * ``VERSION_UNSUPPORTED``—— 资源不存在该版本；
        * ``VERSION_INCOMPATIBLE``—— 版本存在但请求字段与该版本语义不兼容。
        """
        versions = self._routes.get(resource)
        if not versions:
            self._reject(
                ErrorCode.VERSION_UNSUPPORTED,
                f"资源不存在: {resource!r}",
                resource=resource,
                requested_version=version,
            )

        if version is None or version == "":
            self._reject(
                ErrorCode.VERSION_MISSING,
                "请求缺少版本标识 (X-Api-Version)",
                resource=resource,
                supported_versions=self.supported_versions(resource),
            )

        spec = versions.get(version)
        if spec is None:
            self._reject(
                ErrorCode.VERSION_UNSUPPORTED,
                f"资源 {resource!r} 不支持版本 {version!r}",
                resource=resource,
                requested_version=version,
                supported_versions=self.supported_versions(resource),
            )

        # 字段级兼容性：目标版本无法解释的字段按不兼容拒绝，
        # 绝不静默忽略或转交给其他版本。
        unknown = sorted(set(payload) - spec.accepted_fields)
        if unknown:
            self._reject(
                ErrorCode.VERSION_INCOMPATIBLE,
                f"请求字段与版本 {version!r} 不兼容: {unknown}",
                resource=resource,
                requested_version=version,
                incompatible_fields=unknown,
            )

        return spec.handler
