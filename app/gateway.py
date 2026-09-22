"""网关治理编排：限流 -> 版本协商 -> 幂等执行。

顺序与结论（每一步都可能给出可区分的拒绝结论）：

1. 限流：按调用方判定，超限直接拒绝（RATE_LIMITED），并给出 retry_after。
2. 版本协商：解析资源版本并做字段兼容性校验，缺失/不支持/不兼容分别拒绝。
3. 幂等执行：在版本解析出的处理器外包一层幂等，区分首次/复用/冲突/处理中。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.errors import ErrorCode, GatewayError
from app.idempotency import IdempotencyOutcome, IdempotencyStore
from app.ratelimit import RateLimiter
from app.versioning import VersionRegistry

logger = logging.getLogger("app.gateway")


@dataclass
class RequestContext:
    request_id: str
    caller: str
    resource: str
    version: str | None
    idempotency_key: str | None
    payload: dict[str, Any]


class Gateway:
    """组合三个治理能力的统一入口。"""

    def __init__(
        self,
        registry: VersionRegistry,
        limiter: RateLimiter,
        store: IdempotencyStore,
    ) -> None:
        self.registry = registry
        self.limiter = limiter
        self.store = store

    def handle(self, ctx: RequestContext) -> IdempotencyOutcome:
        logger.info(
            "gateway request_id=%s caller=%s resource=%s version=%s idempotency_key=%s",
            ctx.request_id,
            ctx.caller,
            ctx.resource,
            ctx.version,
            ctx.idempotency_key,
        )

        # 1) 限流（判定日志含调用方、配额、窗口、retry_after）
        decision = self.limiter.check(ctx.caller)
        logger.info(
            "gateway request_id=%s rate-limit allowed=%s reason=%s remaining=%s retry_after=%s",
            ctx.request_id,
            decision.allowed,
            decision.reason,
            decision.remaining,
            decision.retry_after,
        )
        if not decision.allowed:
            raise GatewayError(
                ErrorCode.RATE_LIMITED,
                f"调用方 {ctx.caller!r} 在窗口 {decision.window_epoch} 超出配额 {decision.limit}",
                caller=ctx.caller,
                limit=decision.limit,
                window_epoch=decision.window_epoch,
                retry_after=decision.retry_after,
                reason=decision.reason,
            )

        # 2) 版本协商（失败抛 VERSION_* 三类错误）
        handler = self.registry.resolve(ctx.resource, ctx.version, ctx.payload)

        # 3) 幂等执行（内部日志含 request_id 与幂等状态）
        outcome = self.store.execute(
            ctx.idempotency_key, ctx.payload, handler, request_id=ctx.request_id
        )
        logger.info(
            "gateway request_id=%s idempotency status=%s",
            ctx.request_id,
            outcome.status.value,
        )
        # CONFLICT / PROCESSING 以 GatewayError 形式给出可区分的拒绝结论
        if outcome.error is not None:
            raise outcome.error
        return outcome
