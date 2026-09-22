"""网关治理层统一错误定义。

每个错误码对应一种可区分的失败原因，便于调用方判断重试策略。
"""

from __future__ import annotations

import enum


class ErrorCode(enum.Enum):
    """网关可区分错误码。"""

    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"


class GatewayError(Exception):
    """网关治理层拒绝请求时抛出的异常。"""

    def __init__(self, code: ErrorCode, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
