"""网关治理服务：按固定顺序组合版本校验、限流与幂等。

处理顺序：版本校验 -> 限流 -> 幂等申领 -> 执行业务 -> 完成/失败登记。
任一环节拒绝都抛出带可区分错误码的 GatewayError，不会进入后续环节。
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
from gateway.ratelimit import SlidingWindowRateLimiter
from gateway.versioning import VersionRegistry

logger = logging.getLogger("gateway.service")


@dataclass(frozen=True)
class GatewayRequest:
    """进入网关的统一请求。"""

    request_id: str
    caller: str
    version: str
    idempotency_key: str
    payload: dict


@dataclass(frozen=True)
class GatewayResponse:
    """网关放行并处理完成后的结果。"""

    request_id: str
    status: str  # "executed" | "replayed"
    result: Any


def fingerprint_of(payload: dict) -> str:
    """对请求体生成稳定指纹，用于识别幂等键冲突。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class GatewayService:
    """组合版本治理、限流与幂等的入口服务。"""

    def __init__(
        self,
        versions: VersionRegistry,
        limiter: SlidingWindowRateLimiter,
        idempotency: IdempotencyStore,
    ) -> None:
        self._versions = versions
        self._limiter = limiter
        self._idempotency = idempotency

    def handle(self, req: GatewayRequest, handler: Callable[[dict], Any]) -> GatewayResponse:
        # 1. 版本兼容校验
        self._versions.validate(req.version, req.payload)
        # 2. 限流判定
        decision = self._limiter.check(req.caller)
        logger.info(
            "request_id=%s caller=%s rate_limit allowed=%s remaining=%s retry_after=%s",
            req.request_id, req.caller, decision.allowed, decision.remaining, decision.retry_after,
        )
        if not decision.allowed:
            raise GatewayError(
                ErrorCode.RATE_LIMITED,
                f"caller {req.caller!r} exceeded rate limit",
                retry_after=decision.retry_after,
            )
        # 3. 幂等申领
        fp = fingerprint_of(req.payload)
        claim = self._idempotency.claim(req.idempotency_key, fp)
        logger.info(
            "request_id=%s idempotency_key=%s state=%s should_execute=%s",
            req.request_id, req.idempotency_key, claim.state.value, claim.should_execute,
        )
        if claim.state is IdempotencyState.COMPLETED:
            return GatewayResponse(req.request_id, status="replayed", result=claim.cached_result)
        if not claim.should_execute:
            raise GatewayError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                f"idempotency key {req.idempotency_key!r} is being processed",
            )
        # 4. 执行业务并登记结果；失败则释放键以允许安全重试
        try:
            result = handler(req.payload)
        except Exception:
            self._idempotency.fail(req.idempotency_key, fp)
            logger.info("request_id=%s idempotency_key=%s state=FAILED", req.request_id, req.idempotency_key)
            raise
        self._idempotency.complete(req.idempotency_key, fp, result)
        return GatewayResponse(req.request_id, status="executed", result=result)
