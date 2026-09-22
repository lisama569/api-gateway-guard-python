"""统一错误码与异常，保证各类失败结论可区分。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # 版本治理
    VERSION_MISSING = "VERSION_MISSING"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
    # 限流
    RATE_LIMITED = "RATE_LIMITED"
    # 幂等
    IDEMPOTENCY_PROCESSING = "IDEMPOTENCY_PROCESSING"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class GatewayError(Exception):
    """网关治理层抛出的统一异常。"""

    def __init__(self, code: ErrorCode, message: str, **extra: object) -> None:
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)
