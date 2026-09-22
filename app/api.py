"""FastAPI 装配层：把治理层暴露为可本地验证的 HTTP 服务。

资源示例：POST /api/orders
    X-Caller         调用方标识（限流维度）
    X-Api-Version    接口版本（必填：v1 / v2）
    Idempotency-Key  幂等标识（可选）
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ErrorCode, GatewayError
from app.gateway import Gateway, RequestContext
from app.idempotency import IdempotencyStatus, IdempotencyStore
from app.ratelimit import RateLimiter
from app.versioning import VersionRegistry, VersionSpec

logger = logging.getLogger("app.api")


class OrderIn(BaseModel):
    name: str | None = None
    coupon_id: str | None = None
    note: str | None = None


def _order_handler_v1(payload: dict[str, Any]) -> dict[str, Any]:
    # v1：只处理 name，不含 v2 的促销语义
    return {"order_id": uuid.uuid4().hex, "name": payload["name"], "api_version": "v1"}


def _order_handler_v2(payload: dict[str, Any]) -> dict[str, Any]:
    # v2：在 v1 基础上新增 coupon_id（不兼容字段，v1 不得接受）
    discount = "10%" if payload.get("coupon_id") else "0%"
    return {
        "order_id": uuid.uuid4().hex,
        "name": payload["name"],
        "coupon_id": payload.get("coupon_id"),
        "discount": discount,
        "api_version": "v2",
    }


def build_registry() -> VersionRegistry:
    registry = VersionRegistry()
    registry.register(
        "orders",
        VersionSpec("v1", _order_handler_v1, accepted_fields=frozenset({"name"})),
    )
    registry.register(
        "orders",
        VersionSpec(
            "v2", _order_handler_v2, accepted_fields=frozenset({"name", "coupon_id"})
        ),
    )
    return registry


def create_app(
    rate_limit: int = 5,
    window_seconds: int = 60,
) -> FastAPI:
    app = FastAPI(title="API Gateway Guard", version="0.1.0")
    gateway = Gateway(build_registry(), RateLimiter(rate_limit, window_seconds), IdempotencyStore())
    app.state.gateway = gateway

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        status_map = {
            ErrorCode.VERSION_MISSING: 400,
            ErrorCode.VERSION_UNSUPPORTED: 404,
            ErrorCode.VERSION_INCOMPATIBLE: 422,
            ErrorCode.RATE_LIMITED: 429,
            ErrorCode.IDEMPOTENCY_PROCESSING: 409,
            ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        }
        status_code = status_map[exc.code]
        body: dict[str, Any] = {
            "error": {
                "code": exc.code.value,
                "message": exc.message,
                **{k: v for k, v in exc.extra.items() if v is not None},
            }
        }
        headers: dict[str, str] = {}
        if exc.code is ErrorCode.RATE_LIMITED:
            headers["Retry-After"] = str(exc.extra.get("retry_after", ""))
        if exc.code is ErrorCode.IDEMPOTENCY_PROCESSING:
            headers["Retry-After"] = str(exc.extra.get("retry_after_seconds", "1"))
        logger.warning("http reject code=%s status=%s message=%s", exc.code.value, status_code, exc.message)
        return JSONResponse(status_code=status_code, content=body, headers=headers)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/orders")
    async def create_order(request: Request, body: OrderIn) -> JSONResponse:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        caller = request.headers.get("X-Caller", "anonymous")
        version = request.headers.get("X-Api-Version")
        idem_key = request.headers.get("Idempotency-Key")
        # 仅透传客户端实际提供的字段，便于版本字段兼容性校验
        payload = body.model_dump(exclude_none=True)

        ctx = RequestContext(
            request_id=request_id,
            caller=caller,
            resource="orders",
            version=version,
            idempotency_key=idem_key,
            payload=payload,
        )
        outcome = gateway.handle(ctx)
        # PROCESSING 状态由 store 以 GatewayError 抛出；能走到这里的是
        # DIRECT/COMPLETED/REPLAYED/RETRIED 成功结论。
        http_status = 200 if outcome.status in (
            IdempotencyStatus.REPLAYED, IdempotencyStatus.DIRECT
        ) else 201
        return JSONResponse(
            status_code=http_status,
            content={
                "request_id": request_id,
                "idempotency_status": outcome.status.value,
                "data": outcome.result,
            },
            headers={"Idempotency-Status": outcome.status.value},
        )

    return app
