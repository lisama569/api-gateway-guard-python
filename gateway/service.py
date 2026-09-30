"""网关治理服务：按固定顺序组合版本生命周期、配额与幂等。

处理顺序：版本解析（缺失/非法/不认识/停用）-> 请求体兼容校验
-> 按 (caller, version) 作用域配额 -> 按 (caller, version) 作用域幂等申领
-> 执行业务 -> 完成/失败登记。
任一环节拒绝都抛出带可区分错误码的 GatewayError，不会进入后续环节。
废弃期版本的成功响应附带迁移公告（sunset / replacement）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gateway.errors import ErrorCode, GatewayError
from gateway.idempotency import IdempotencyState, IdempotencyStore
from gateway.ratelimit import (
    ScopedRateLimitDecision,
    ScopedSlidingWindowRateLimiter,
    SlidingWindowRateLimiter,
)
from gateway.versioning import DeprecationNotice, VersionRegistry

logger = logging.getLogger("gateway.service")


@dataclass(frozen=True)
class GatewayRequest:
    """进入网关的统一请求。"""

    request_id: str
    caller: str
    idempotency_key: str
    payload: dict
    #: 未声明版本时为 None，由版本解析环节给出 MISSING_VERSION 结论。
    version: str | None = None


@dataclass(frozen=True)
class GatewayResponse:
    """网关放行并处理完成后的结果。"""

    request_id: str
    status: str  # "executed" | "replayed"
    result: Any
    #: 废弃期版本随成功响应返回的迁移公告；ACTIVE 版本为 None。
    deprecation: DeprecationNotice | None = None


def fingerprint_of(payload: dict) -> str:
    """对请求体生成稳定指纹，用于识别幂等键冲突。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class GatewayService:
    """组合版本治理、限流与幂等的入口服务。"""

    def __init__(
        self,
        versions: VersionRegistry,
        limiter: SlidingWindowRateLimiter | ScopedSlidingWindowRateLimiter,
        idempotency: IdempotencyStore,
    ) -> None:
        self._versions = versions
        self._limiter = limiter
        self._idempotency = idempotency

    def handle(self, req: GatewayRequest, handler: Callable[[dict], Any]) -> GatewayResponse:
        # 1. 版本生命周期解析（缺失 / 非法 / 不认识 / 已停用在此分流）
        resolved = self._versions.resolve(req.version)
        # 2. 请求体与声明版本的兼容性校验
        self._versions.check_payload(resolved.version, req.payload)
        logger.info(
            "request_id=%s caller=%s version=%s lifecycle=%s",
            req.request_id, req.caller, resolved.version, resolved.status.value,
        )
        # 3. 配额判定：新限流器按 (caller, version) 作用域；旧限流器仅按 caller。
        if isinstance(self._limiter, ScopedSlidingWindowRateLimiter):
            decision = self._limiter.check(req.caller, resolved.version)
            self._log_scoped_decision(req, decision)
            if not decision.allowed:
                raise GatewayError(
                    ErrorCode.RATE_LIMITED,
                    f"caller {req.caller!r} exceeded quota for version {resolved.version!r}",
                    retry_after=decision.retry_after,
                    details={
                        "scope": {"caller": req.caller, "version": resolved.version},
                        "limit": decision.limit,
                        "remaining": decision.remaining,
                        "matched_rule": decision.source,
                    },
                )
        else:
            decision = self._limiter.check(req.caller)
            logger.info(
                "request_id=%s caller=%s rate_limit allowed=%s remaining=%s retry_after=%s",
                req.request_id, req.caller, decision.allowed,
                decision.remaining, decision.retry_after,
            )
            if not decision.allowed:
                raise GatewayError(
                    ErrorCode.RATE_LIMITED,
                    f"caller {req.caller!r} exceeded rate limit",
                    retry_after=decision.retry_after,
                )
        # 4. 幂等申领：作用域为 (key, caller, version)，跨版本/跨调用方互不回放
        fp = fingerprint_of(req.payload)
        claim = self._idempotency.claim(
            req.idempotency_key, fp, caller=req.caller, version=resolved.version
        )
        logger.info(
            "request_id=%s idempotency_key=%s caller=%s version=%s state=%s should_execute=%s",
            req.request_id, req.idempotency_key, req.caller,
            resolved.version, claim.state.value, claim.should_execute,
        )
        if claim.state is IdempotencyState.COMPLETED:
            return GatewayResponse(
                req.request_id, status="replayed",
                result=claim.cached_result, deprecation=resolved.notice,
            )
        if not claim.should_execute:
            raise GatewayError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                f"idempotency key {req.idempotency_key!r} is being processed",
            )
        # 5. 执行业务并登记结果；失败则释放键以允许同作用域安全重试
        try:
            result = handler(req.payload)
        except Exception:
            self._idempotency.fail(
                req.idempotency_key, fp, caller=req.caller, version=resolved.version
            )
            logger.info(
                "request_id=%s idempotency_key=%s state=FAILED",
                req.request_id, req.idempotency_key,
            )
            raise
        self._idempotency.complete(
            req.idempotency_key, fp, result,
            caller=req.caller, version=resolved.version,
        )
        return GatewayResponse(
            req.request_id, status="executed",
            result=result, deprecation=resolved.notice,
        )

    @staticmethod
    def _log_scoped_decision(req: GatewayRequest, d: ScopedRateLimitDecision) -> None:
        logger.info(
            "request_id=%s caller=%s version=%s quota allowed=%s "
            "limit=%s remaining=%s source=%s retry_after=%s",
            req.request_id, req.caller, d.scope[1], d.allowed,
            d.limit, d.remaining, d.source, d.retry_after,
        )
