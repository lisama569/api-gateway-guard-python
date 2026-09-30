"""幂等处理存储。

契约：
- claim：首次申领返回 should_execute=True；处理中重复申领返回
  IN_PROGRESS（不执行）；已完成返回 COMPLETED 并复用缓存结果；
  失败后重新申领视为安全重试，返回 should_execute=True。
- 同一幂等键对应不同的请求指纹（fingerprint）即标识冲突，
  抛出 IDEMPOTENCY_CONFLICT，与"处理中"明确区分。
- 所有状态迁移在同一把锁内完成，并发下不会重复触发副作用。

跨版本/跨调用方隔离：
- 通过 scope(caller, version) 获得作用域视图；幂等标识只在
  "同一调用方 + 同一接口版本"作用域内有意义。
- 换版本或换调用方复用同一标识是互不相关的两笔操作：
  不会回放旧结果、不会误判处理中，也不会误判冲突。
- 失败后的安全重试同样只在相同作用域内允许。
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import Any

from gateway.errors import ErrorCode, GatewayError

# 键作用域内部分隔符（ASCII US），不会出现在正常调用方/版本标识中
_SCOPE_SEP = "\x1f"


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

    def scope(self, caller: str, version: str) -> ScopedIdempotencyView:
        """返回绑定 (caller, version) 作用域的幂等视图。"""
        return ScopedIdempotencyView(self, IdempotencyScope(caller, version))

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


@dataclass(frozen=True)
class IdempotencyScope:
    """幂等作用域：同一调用方 + 同一接口版本。"""

    caller: str
    version: str

    def qualify(self, key: str) -> str:
        """把业务幂等键映射为作用域内的存储键，不同作用域必然不同。"""
        return f"{self.caller}{_SCOPE_SEP}{self.version}{_SCOPE_SEP}{key}"


class ScopedIdempotencyView:
    """绑定 (caller, version) 作用域的幂等视图，接口与 IdempotencyStore 键级 API 一致。"""

    def __init__(self, store: IdempotencyStore, scope: IdempotencyScope) -> None:
        self._store = store
        self.scope = scope

    def claim(self, key: str, fingerprint: str) -> IdempotencyClaim:
        return self._store.claim(self.scope.qualify(key), fingerprint)

    def complete(self, key: str, fingerprint: str, result: Any) -> None:
        self._store.complete(self.scope.qualify(key), fingerprint, result)

    def fail(self, key: str, fingerprint: str) -> None:
        self._store.fail(self.scope.qualify(key), fingerprint)

    def state_of(self, key: str) -> IdempotencyState | None:
        return self._store.state_of(self.scope.qualify(key))
