"""FastAPI 入口：把网关治理层暴露为 HTTP 接口。

错误码到 HTTP 状态的映射（响应体内仍携带可区分的 code）：
- MISSING_VERSION        -> 400
- INVALID_VERSION        -> 400
- UNSUPPORTED_VERSION    -> 400
- VERSION_RETIRED        -> 410（带替代版本信息）
- INCOMPATIBLE_VERSION   -> 422
- RATE_LIMITED           -> 429（带 Retry-After 头与命中配额范围）
- IDEMPOTENCY_CONFLICT   -> 409
- IDEMPOTENCY_IN_PROGRESS -> 409

废弃期版本的成功（201）响应额外带：
- Deprecation: true
- Sunset: 计划停止日期（HTTP-date）
- Link: 替代版本资源链接
同时响应体内含 deprecation 对象。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from email.utils import formatdate

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    QuotaPolicy,
    ScopedSlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayService

_STATUS_BY_CODE = {
    ErrorCode.MISSING_VERSION: 400,
    ErrorCode.INVALID_VERSION: 400,
    ErrorCode.UNSUPPORTED_VERSION: 400,
    ErrorCode.VERSION_RETIRED: 410,
    ErrorCode.INCOMPATIBLE_VERSION: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
}


def build_service() -> GatewayService:
    versions = VersionRegistry()
    # v0 已停止提供；v1 处于废弃期（引导迁往 v2）；v2 正常提供。
    versions.register(VersionRule("v0", status=VersionStatus.RETIRED, replacement="v2"))
    versions.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset="2027-03-01",
            replacement="v2",
        )
    )
    versions.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    policy = QuotaPolicy(
        default_limit=100,
        version_limits={"v1": 50},  # 老版本统一收紧
        caller_version_limits={("migration-partner", "v1"): 5},  # 特定调用方单独覆盖
    )
    return GatewayService(
        versions=versions,
        limiter=ScopedSlidingWindowRateLimiter(policy=policy, window_seconds=60.0),
        idempotency=IdempotencyStore(),
    )


app = FastAPI(title="api-gateway-guard")
service = build_service()


class OrderIn(BaseModel):
    caller: str
    #: 可选：缺省时由网关给出 MISSING_VERSION 的稳定结论。
    version: str | None = None
    idempotency_key: str
    payload: dict


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = f"{exc.retry_after:.3f}"
    if exc.code is ErrorCode.VERSION_RETIRED and exc.details.get("replacement"):
        replacement = exc.details["replacement"]
        headers["Link"] = f'</orders?version={replacement}>; rel="successor-version"'
    body = {
        "code": exc.code.value,
        "message": exc.message,
        "retry_after": exc.retry_after,
    }
    if exc.details:
        body["details"] = exc.details
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
    headers = {}
    deprecation = None
    if resp.deprecation is not None:
        sunset_dt = datetime.fromisoformat(resp.deprecation.sunset).replace(tzinfo=timezone.utc)
        sunset_http = formatdate(sunset_dt.timestamp(), usegmt=True)
        headers["Deprecation"] = "true"
        headers["Sunset"] = sunset_http
        headers["Link"] = (
            f'</orders?version={resp.deprecation.replacement}>; rel="successor-version"'
        )
        deprecation = {
            "status": "DEPRECATED",
            "sunset": resp.deprecation.sunset,
            "replacement": resp.deprecation.replacement,
        }
    return JSONResponse(
        status_code=201,
        content={
            "request_id": resp.request_id,
            "status": resp.status,
            "result": resp.result,
            "deprecation": deprecation,
        },
        headers=headers,
    )
