"""API 网关治理层：版本治理、限流、幂等。"""

from gateway.errors import ErrorCode, GatewayError
from gateway.idempotency import (
    IdempotencyClaim,
    IdempotencyScope,
    IdempotencyState,
    IdempotencyStore,
    ScopedIdempotencyView,
)
from gateway.quota import QuotaDecision, QuotaLimiter, QuotaRule, QuotaScope
from gateway.ratelimit import RateLimitDecision, SlidingWindowRateLimiter
from gateway.versioning import (
    DeprecationNotice,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)

__all__ = [
    "DeprecationNotice",
    "ErrorCode",
    "GatewayError",
    "IdempotencyClaim",
    "IdempotencyScope",
    "IdempotencyState",
    "IdempotencyStore",
    "ScopedIdempotencyView",
    "QuotaDecision",
    "QuotaLimiter",
    "QuotaRule",
    "QuotaScope",
    "RateLimitDecision",
    "SlidingWindowRateLimiter",
    "VersionRegistry",
    "VersionRule",
    "VersionStatus",
]
