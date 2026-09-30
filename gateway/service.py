"""网关治理服务：按固定顺序组合版本校验/生命周期、配额与跨版本幂等。

处理顺序：版本校验 -> 配额判定 -> 作用域幂等申领 -> 执行业务 -> 完成/失败登记。
任一环节拒绝都抛出带可区分错误码的 GatewayError，不会进入后续环节。

- 配额按 (caller, version) 组合独立计数；拒绝时携带命中范围、剩余次数、等待时间。
- 幂等作用域为 (caller, version)：同一标识跨版本/跨调用方不回放、不冲突。
- 命中废弃版本时响应附带 deprecation 迁移提示。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gateway.errors import ErrorCode, GatewayError
from gateway.idempotency import IdempotencyState, IdempotencyStore
from gateway.quota import QuotaDecision, QuotaLimiter, QuotaScope
from gateway.ratelimit import SlidingWindowRateLimiter
from gateway.versioning import DeprecationNotice, VersionRegistry

logger = logging.getLogger("gateway.service")


@dataclass(frozen=True)
class GatewayRequest:
    """进入网关的统一请求。version 允许为 None（未声明版本）。"""

    request_id: str
    caller: str
    version: str | None
    idempotency_key: str
    payload: dict


@dataclass(frozen=True)
class GatewayResponse:
    """网关放行并处理完成后的结果。"""

    request_id: str
    status: str  # "executed" | "replayed"
    result: Any
    deprecation: DeprecationNotice | None = None
    warnings: list[dict] = field(default_factory=list)


def fingerprint_of(payload: dict) -> str:
    """对请求体生成稳定指纹，用于识别幂等键冲突。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class _LegacyLimiterAdapter:
    """把旧的按 caller 限流器适配成 (caller, version) 配额接口。

    旧限流器不区分版本，语义上等价于"调用方整体额度"，命中范围标记为 CALLER。
    """

    def __init__(self, limiter: SlidingWindowRateLimiter) -> None:
        self._limiter = limiter

    def check(self, caller: str, version: str, now: float | None = None) -> QuotaDecision:
        d = self._limiter.check(caller) if now is None else self._limiter.check(caller, now=now)
        return QuotaDecision(
            allowed=d.allowed,
            remaining=d.remaining,
            retry_after=d.retry_after,
            scope=QuotaScope.CALLER,
            limit=self._limiter.limit,
            caller=caller,
            version=version,
        )


class GatewayService:
    """组合版本治理、配额与幂等的入口服务。"""

    def __init__(
        self,
        versions: VersionRegistry,
        limiter: SlidingWindowRateLimiter | QuotaLimiter,
        idempotency: IdempotencyStore,
    ) -> None:
        self._versions = versions
        self._idempotency = idempotency
        self._limiter: QuotaLimiter | _LegacyLimiterAdapter = self._adapt(limiter)

    @staticmethod
    def _adapt(
        value: SlidingWindowRateLimiter | QuotaLimiter,
    ) -> QuotaLimiter | _LegacyLimiterAdapter:
        return value if isinstance(value, QuotaLimiter) else _LegacyLimiterAdapter(value)

    def __setattr__(self, name: str, value: Any) -> None:
        # 允许测试/装配代码直接替换 _limiter：旧限流器自动适配为配额接口
        if name == "_limiter" and not isinstance(value, _LegacyLimiterAdapter):
            value = self._adapt(value)
        super().__setattr__(name, value)

    @property
    def limiter(self) -> QuotaLimiter | _LegacyLimiterAdapter:
        """当前生效的配额组件（旧限流器会被透明适配）。"""
        return self._limiter

    def handle(self, req: GatewayRequest, handler: Callable[[dict], Any]) -> GatewayResponse:
        # 1. 版本生命周期与兼容校验（未声明/格式非法/不认识/已停用/形状不符在此区分）
        self._versions.validate(req.version, req.payload)
        version: str = req.version  # validate 已保证非 None 且合法
        notice = self._versions.deprecation_notice(version)
        # 2. (caller, version) 配额判定
        decision = self._limiter.check(req.caller, version)
        logger.info(
            "request_id=%s caller=%s version=%s rate_limit scope=%s allowed=%s "
            "remaining=%s retry_after=%s",
            req.request_id, req.caller, version, decision.scope.value,
            decision.allowed, decision.remaining, decision.retry_after,
        )
        if not decision.allowed:
            raise GatewayError(
                ErrorCode.RATE_LIMITED,
                (
                    f"caller {req.caller!r} exceeded caller quota on version {version!r}"
                    if decision.scope is QuotaScope.CALLER
                    else f"version {version!r} shared quota exceeded for caller {req.caller!r}"
                ),
                retry_after=decision.retry_after,
                remaining=decision.remaining,
                scope=decision.scope.value,
            )
        # 3. (caller, version) 作用域幂等申领
        scoped = self._idempotency.scope(req.caller, version)
        fp = fingerprint_of(req.payload)
        claim = scoped.claim(req.idempotency_key, fp)
        logger.info(
            "request_id=%s caller=%s version=%s idempotency_key=%s state=%s should_execute=%s",
            req.request_id, req.caller, version, req.idempotency_key,
            claim.state.value, claim.should_execute,
        )
        if claim.state is IdempotencyState.COMPLETED:
            return self._respond(req, "replayed", claim.cached_result, notice)
        if not claim.should_execute:
            raise GatewayError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                f"idempotency key {req.idempotency_key!r} is being processed "
                f"for caller={req.caller!r} version={version!r}",
            )
        # 4. 执行业务并登记结果；失败则释放键以允许同作用域安全重试
        try:
            result = handler(req.payload)
        except Exception:
            scoped.fail(req.idempotency_key, fp)
            logger.info(
                "request_id=%s idempotency_key=%s state=FAILED",
                req.request_id, req.idempotency_key,
            )
            raise
        scoped.complete(req.idempotency_key, fp, result)
        return self._respond(req, "executed", result, notice)

    @staticmethod
    def _respond(
        req: GatewayRequest,
        status: str,
        result: Any,
        notice: DeprecationNotice | None,
    ) -> GatewayResponse:
        warnings = [notice.as_dict()] if notice is not None else []
        return GatewayResponse(
            request_id=req.request_id,
            status=status,
            result=result,
            deprecation=notice,
            warnings=warnings,
        )
