"""幂等处理存储。

契约：
- claim：首次申领返回 should_execute=True；处理中重复申领返回
  IN_PROGRESS（不执行）；已完成返回 COMPLETED 并复用缓存结果；
  失败后重新申领视为安全重试，返回 should_execute=True。
- 同一幂等键对应不同的请求指纹（fingerprint）即标识冲突，
  抛出 IDEMPOTENCY_CONFLICT，与"处理中"明确区分。
- 所有状态迁移在同一把锁内完成，并发下不会重复触发副作用。
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import Any

from gateway.errors import ErrorCode, GatewayError


class IdempotencyState(enum.Enum):
    """幂等记录状态。"""

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class IdempotencyClaim:
    """一次幂等申领的结果。"""

    state: IdempotencyState
    should_execute: bool
    cached_result: Any = None


@dataclass
class _Record:
    fingerprint: str
    state: IdempotencyState
    result: Any = None


class IdempotencyStore:
    """幂等键存储：申领、完成、失败释放。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}

    def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = _Record(fingerprint, IdempotencyState.IN_PROGRESS)
                return IdempotencyClaim(IdempotencyState.IN_PROGRESS, should_execute=True)
            if rec.fingerprint != fingerprint:
                raise GatewayError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    f"idempotency key {key!r} reused with a different request",
                )
            if rec.state is IdempotencyState.IN_PROGRESS:
                return IdempotencyClaim(IdempotencyState.IN_PROGRESS, should_execute=False)
            if rec.state is IdempotencyState.COMPLETED:
                return IdempotencyClaim(
                    IdempotencyState.COMPLETED, should_execute=False, cached_result=rec.result
                )
            # FAILED：允许安全重试，重新进入处理中
            rec.state = IdempotencyState.IN_PROGRESS
            return IdempotencyClaim(IdempotencyState.IN_PROGRESS, should_execute=True)

    def complete(self, key: str, fingerprint: str, result: Any) -> None:
        with self._lock:
            rec = self._require_owned(key, fingerprint)
            rec.state = IdempotencyState.COMPLETED
            rec.result = result

    def fail(self, key: str, fingerprint: str) -> None:
        """标记处理失败，释放该键以允许后续安全重试。"""
        with self._lock:
            rec = self._require_owned(key, fingerprint)
            rec.state = IdempotencyState.FAILED

    def state_of(self, key: str) -> IdempotencyState | None:
        with self._lock:
            rec = self._records.get(key)
            return rec.state if rec else None

    def _require_owned(self, key: str, fingerprint: str) -> _Record:
        rec = self._records.get(key)
        if rec is None:
            raise GatewayError(ErrorCode.IDEMPOTENCY_CONFLICT, f"unknown idempotency key {key!r}")
        if rec.fingerprint != fingerprint:
            raise GatewayError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} reused with a different request",
            )
        return rec
