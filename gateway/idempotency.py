"""幂等处理存储。

契约：
- claim：首次申领返回 should_execute=True；处理中重复申领返回
  IN_PROGRESS（不执行）；已完成返回 COMPLETED 并复用缓存结果；
  失败后重新申领视为安全重试，返回 should_execute=True。
- 同一幂等键对应不同的请求指纹（fingerprint）即标识冲突，
  抛出 IDEMPOTENCY_CONFLICT，与"处理中"明确区分。
- 所有状态迁移在同一把锁内完成，并发下不会重复触发副作用。

跨版本 / 跨调用方隔离：
- 幂等作用域为 (idempotency_key, caller, version) 三元组。
- 同一幂等键只有在同一调用方、同一接口版本下重复提交时才复用结果或
  识别处理中；换版本或换调用方再用该键视为全新操作，不回放旧结果，
  也不会把不同作用域的请求误判成冲突。
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
    """幂等键存储：申领、完成、失败释放。

    记录按 (key, caller, version) 三元组隔离；未显式传作用域时
    统一落到默认作用域，保持旧调用方行为不变。
    """

    #: 未显式声明作用域时使用的占位作用域。
    DEFAULT_SCOPE = ("*", "*")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, str], _Record] = {}

    @classmethod
    def _scoped(cls, key: str, caller: str, version: str) -> tuple[str, str, str]:
        return (key, caller, version)

    def claim(
        self,
        key: str,
        fingerprint: str,
        *,
        caller: str = DEFAULT_SCOPE[0],
        version: str = DEFAULT_SCOPE[1],
    ) -> IdempotencyClaim:
        with self._lock:
            scoped = self._scoped(key, caller, version)
            rec = self._records.get(scoped)
            if rec is None:
                self._records[scoped] = _Record(fingerprint, IdempotencyState.IN_PROGRESS)
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

    def complete(
        self,
        key: str,
        fingerprint: str,
        result: Any,
        *,
        caller: str = DEFAULT_SCOPE[0],
        version: str = DEFAULT_SCOPE[1],
    ) -> None:
        with self._lock:
            rec = self._require_owned(key, fingerprint, caller, version)
            rec.state = IdempotencyState.COMPLETED
            rec.result = result

    def fail(
        self,
        key: str,
        fingerprint: str,
        *,
        caller: str = DEFAULT_SCOPE[0],
        version: str = DEFAULT_SCOPE[1],
    ) -> None:
        """标记处理失败，释放该键以允许后续安全重试。"""
        with self._lock:
            rec = self._require_owned(key, fingerprint, caller, version)
            rec.state = IdempotencyState.FAILED

    def state_of(
        self,
        key: str,
        *,
        caller: str = DEFAULT_SCOPE[0],
        version: str = DEFAULT_SCOPE[1],
    ) -> IdempotencyState | None:
        with self._lock:
            rec = self._records.get(self._scoped(key, caller, version))
            return rec.state if rec else None

    def _require_owned(
        self, key: str, fingerprint: str, caller: str, version: str
    ) -> _Record:
        scoped = self._scoped(key, caller, version)
        rec = self._records.get(scoped)
        if rec is None:
            raise GatewayError(ErrorCode.IDEMPOTENCY_CONFLICT, f"unknown idempotency key {key!r}")
        if rec.fingerprint != fingerprint:
            raise GatewayError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} reused with a different request",
            )
        return rec
