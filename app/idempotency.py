"""幂等处理：同一幂等标识的重复请求不重复触发副作用。

契约（明确、可解释）：

* 请求可通过 ``Idempotency-Key`` 头携带幂等标识；未携带则不启用幂等，
  直接执行（结论状态为 DIRECT）。
* 首次携带 key 的请求：原子占位为 PROCESSING 并执行业务，成功后落库为
  COMPLETED 并缓存结果（返回 COMPLETED）；之后携带相同 key 的请求**不再
  执行 handler**，直接复用结果（REPLAYED）。
* 相同 key 但请求体指纹不同：判为 CONFLICT，明确拒绝，不执行 handler。
* 首个请求仍在处理中时到达的重复请求：返回 PROCESSING（可重试），不并发
  执行第二个 handler。
* 业务处理中途失败：记录置为 FAILED，后续携带相同 key+payload 的请求可
  安全重试（重新执行，成功后返回 RETRIED）；携带不同 payload 仍判 CONFLICT。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app.errors import ErrorCode, GatewayError

logger = logging.getLogger("app.idempotency")

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class IdempotencyStatus(str, Enum):
    DIRECT = "DIRECT"            # 未携带幂等键，直接执行
    PROCESSING = "PROCESSING"    # 同键前序请求处理中（可重试）
    COMPLETED = "COMPLETED"      # 首次执行成功，结果刚产生
    REPLAYED = "REPLAYED"        # 已完成，复用缓存结果
    CONFLICT = "CONFLICT"        # 同 key 但请求指纹不同
    RETRIED = "RETRIED"          # 前序失败后重新执行并成功
    FAILED = "FAILED"            # 前序执行失败（仅用于内部记录状态）


@dataclass
class IdempotencyRecord:
    key: str
    fingerprint: str
    status: IdempotencyStatus
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class IdempotencyOutcome:
    """一次幂等包装的执行结论。"""

    status: IdempotencyStatus
    result: dict[str, Any] | None = None
    record: IdempotencyRecord | None = None
    error: GatewayError | None = None


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    """对请求体生成稳定指纹（键排序、紧凑分隔避免歧义）。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """进程内幂等存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, IdempotencyRecord] = {}

    def execute(
        self,
        key: str | None,
        payload: dict[str, Any],
        handler: Handler,
        request_id: str = "-",
    ) -> IdempotencyOutcome:
        """以幂等方式执行 handler，返回可区分的结构化结论。"""
        if key is None or key == "":
            logger.info("idempotency request_id=%s key=<none> status=DIRECT", request_id)
            return IdempotencyOutcome(status=IdempotencyStatus.DIRECT, result=handler(payload))

        fingerprint = canonical_fingerprint(payload)

        # 阶段1：锁内完成判定与原子占位；handler 在锁外执行。
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                if record.fingerprint != fingerprint:
                    logger.warning(
                        "idempotency request_id=%s key=%s status=CONFLICT", request_id, key
                    )
                    return IdempotencyOutcome(
                        status=IdempotencyStatus.CONFLICT,
                        record=record,
                        error=GatewayError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            f"幂等键 {key!r} 已绑定其他请求体",
                            idempotency_key=key,
                        ),
                    )
                if record.status is IdempotencyStatus.PROCESSING:
                    logger.info(
                        "idempotency request_id=%s key=%s status=PROCESSING", request_id, key
                    )
                    return IdempotencyOutcome(
                        status=IdempotencyStatus.PROCESSING,
                        record=record,
                        error=GatewayError(
                            ErrorCode.IDEMPOTENCY_PROCESSING,
                            "相同幂等键的请求正在处理中，请稍后重试",
                            idempotency_key=key,
                            retry_after_seconds=1,
                        ),
                    )
                if record.status is IdempotencyStatus.COMPLETED:
                    logger.info(
                        "idempotency request_id=%s key=%s status=REPLAYED", request_id, key
                    )
                    return IdempotencyOutcome(
                        status=IdempotencyStatus.REPLAYED,
                        result=record.result,
                        record=record,
                    )
                # record.status is FAILED：同一 payload 允许安全重试，
                # 原子地重新占位。
                record.status = IdempotencyStatus.PROCESSING
                record.error = None
                is_retry = True
            else:
                record = IdempotencyRecord(
                    key=key, fingerprint=fingerprint, status=IdempotencyStatus.PROCESSING
                )
                self._records[key] = record
                is_retry = False

        # 阶段2：锁外执行真实副作用
        try:
            result = handler(payload)
        except Exception as exc:
            with self._lock:
                current = self._records.get(key)
                if current is record and current.status is IdempotencyStatus.PROCESSING:
                    current.status = IdempotencyStatus.FAILED
                    current.error = repr(exc)
            logger.error(
                "idempotency request_id=%s key=%s status=FAILED error=%r retry_safe=True",
                request_id,
                key,
                exc,
            )
            raise

        # 阶段3：成功落库
        with self._lock:
            record.status = IdempotencyStatus.COMPLETED
            record.result = result
            record.error = None

        outcome_status = IdempotencyStatus.RETRIED if is_retry else IdempotencyStatus.COMPLETED
        logger.info(
            "idempotency request_id=%s key=%s status=%s", request_id, key, outcome_status.value
        )
        return IdempotencyOutcome(status=outcome_status, result=result, record=record)
