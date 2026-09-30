"""网关治理层统一错误定义。

每个错误码对应一种可区分的失败原因，便于调用方判断重试策略。
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCode(enum.Enum):
    """网关可区分错误码。"""

    # 版本生命周期（四者必须互可区分）
    MISSING_VERSION = "MISSING_VERSION"          # 请求未声明版本
    INVALID_VERSION = "INVALID_VERSION"          # 版本值格式不合法
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"  # 版本格式合法但网关注册表里不认识
    VERSION_RETIRED = "VERSION_RETIRED"          # 版本曾提供，现已停止提供
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"  # 请求体与声明版本对不上
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"


class GatewayError(Exception):
    """网关治理层拒绝请求时抛出的异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        # 结构化拒绝详情：配额范围/剩余次数、停用版本替代指引等，
        # 方便调用方不依赖字符串解析即可程序化判断。
        self.details: dict[str, Any] = dict(details) if details else {}
