"""FastAPI 入口：把网关治理层暴露为 HTTP 接口。

错误码到 HTTP 状态的映射（响应体内仍携带可区分的 code）：
- VERSION_MISSING          -> 400
- VERSION_MALFORMED        -> 400
- UNSUPPORTED_VERSION      -> 400
- VERSION_RETIRED          -> 410
- INCOMPATIBLE_VERSION     -> 422
- RATE_LIMITED             -> 429（带 Retry-After 头，body 含 scope/remaining）
- IDEMPOTENCY_CONFLICT     -> 409
- IDEMPOTENCY_IN_PROGRESS  -> 409

命中已废弃但仍可用的版本时，正常响应体携带 warnings[0]：
废弃状态、计划停止日期、替代版本与迁移提示。
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    QuotaLimiter,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayService

_STATUS_BY_CODE = {
    ErrorCode.VERSION_MISSING: 400,
    ErrorCode.VERSION_MALFORMED: 400,
    ErrorCode.UNSUPPORTED_VERSION: 400,
    ErrorCode.VERSION_RETIRED: 410,
    ErrorCode.INCOMPATIBLE_VERSION: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
}


def build_service() -> GatewayService:
    versions = VersionRegistry()
    # v1：已废弃但仍可用，计划 2027-03-01 后停止，调用方应迁移到 v2
    versions.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset_on=datetime.date(2027, 3, 1),
            successor="v2",
        )
    )
    versions.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    # v0：已停止提供
    versions.register(VersionRule("v0", status=VersionStatus.RETIRED, successor="v2"))
    # 配额：v1 每 60s 统一 100 次；v2 每 60s 统一 100 次，alice 单独覆盖为 200 次
    quota = QuotaLimiter()
    quota.set_version_limit("v1", limit=100, window_seconds=60.0)
    quota.set_version_limit("v2", limit=100, window_seconds=60.0)
    quota.override_caller_limit("v2", "alice", limit=200)
    return GatewayService(
        versions=versions,
        limiter=quota,
        idempotency=IdempotencyStore(),
    )


app = FastAPI(title="api-gateway-guard")
service = build_service()


class OrderIn(BaseModel):
    caller: str
    version: str | None = None
    idempotency_key: str
    payload: dict


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = f"{exc.retry_after:.3f}"
    body: dict = {
        "code": exc.code.value,
        "message": exc.message,
        "retry_after": exc.retry_after,
    }
    if exc.scope is not None:
        body["scope"] = exc.scope
        body["remaining"] = exc.remaining
    return JSONResponse(
        status_code=_STATUS_BY_CODE[exc.code],
        content=body,
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
    return {
        "request_id": resp.request_id,
        "status": resp.status,
        "result": resp.result,
        "warnings": resp.warnings,
    }
