"""API 网关治理层：版本生命周期、按作用域配额、跨版本幂等。"""

from gateway.errors import ErrorCode, GatewayError
from gateway.idempotency import IdempotencyClaim, IdempotencyState, IdempotencyStore
from gateway.ratelimit import (
    QuotaPolicy,
    RateLimitDecision,
    ScopedRateLimitDecision,
    ScopedSlidingWindowRateLimiter,
    SlidingWindowRateLimiter,
)
from gateway.versioning import (
    DeprecationNotice,
    ResolvedVersion,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)

__all__ = [
    "DeprecationNotice",
    "ErrorCode",
    "GatewayError",
    "IdempotencyClaim",
    "IdempotencyState",
    "IdempotencyStore",
    "QuotaPolicy",
    "RateLimitDecision",
    "ResolvedVersion",
    "ScopedRateLimitDecision",
    "ScopedSlidingWindowRateLimiter",
    "SlidingWindowRateLimiter",
    "VersionRegistry",
    "VersionRule",
    "VersionStatus",
]
