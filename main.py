"""FastAPI 入口：把网关治理层暴露为 HTTP 接口。

错误码到 HTTP 状态的映射（响应体内仍携带可区分的 code）：
- UNSUPPORTED_VERSION   -> 400
- INCOMPATIBLE_VERSION  -> 422
- RATE_LIMITED          -> 429（带 Retry-After 头）
- IDEMPOTENCY_CONFLICT  -> 409
- IDEMPOTENCY_IN_PROGRESS -> 409
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    SlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
)
from gateway.service import GatewayRequest, GatewayService

_STATUS_BY_CODE = {
    ErrorCode.UNSUPPORTED_VERSION: 400,
    ErrorCode.INCOMPATIBLE_VERSION: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
}


def build_service() -> GatewayService:
    versions = VersionRegistry()
    versions.register(VersionRule("v1", required_fields=frozenset({"name"})))
    versions.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    return GatewayService(
        versions=versions,
        limiter=SlidingWindowRateLimiter(limit=100, window_seconds=60.0),
        idempotency=IdempotencyStore(),
    )


app = FastAPI(title="api-gateway-guard")
service = build_service()


class OrderIn(BaseModel):
    caller: str
    version: str
    idempotency_key: str
    payload: dict


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = f"{exc.retry_after:.3f}"
    return JSONResponse(
        status_code=_STATUS_BY_CODE[exc.code],
        content={"code": exc.code.value, "message": exc.message, "retry_after": exc.retry_after},
        headers=headers,
    )


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn):
    req = GatewayRequest(
        request_id=str(uuid.uuid4()),
        caller=body.caller,
        version=body.version,
        idempotency_key=body.idempotency_key,
        payload=body.payload,
    )
    resp = service.handle(req, handler=lambda p: {"order_id": uuid.uuid4().hex, "echo": p})
    return {"request_id": resp.request_id, "status": resp.status, "result": resp.result}
