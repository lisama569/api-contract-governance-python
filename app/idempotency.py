"""幂等键存储：相同 Idempotency-Key 的重复/并发请求只执行一次真实副作用。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.errors import APIError, ErrorCode
from app.logging_setup import get_logger

logger = get_logger("contract.idempotency")


@dataclass
class IdempotencyEntry:
    key: str
    fingerprint: str
    status_code: int
    body: Any
    replay: bool = False  # True 表示这是重放命中（非首次执行）
    created_trace_id: str = ""


@dataclass
class IdempotencyResult:
    entry: IdempotencyEntry
    replayed: bool  # True=命中既有结果；False=本次是首次执行


class IdempotencyStore:
    """进程内幂等存储；每个 key 一把锁，保证并发下只有一个执行者。"""

    def __init__(self) -> None:
        self._entries: dict[str, IdempotencyEntry] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.execution_count = 0  # 测试断言用：真实执行次数

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def run_once(
        self,
        key: str,
        fingerprint: str,
        producer: Callable[[], tuple[int, Any]],
        trace_id: str,
    ) -> IdempotencyResult:
        """幂等地执行 producer；同键同指纹并发/重复请求拿到同一份结果。

        同键不同指纹 -> APIError(IDEMPOTENCY_REUSE)。
        producer 抛异常时不落盘结果（键不被失败请求占用），异常原样上抛。
        """
        lock = self._lock_for(key)
        logger.debug("等待幂等锁 key=%s fingerprint=%s", key, fingerprint)
        with lock:
            existing = self._entries.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    logger.info(
                        "幂等键复用冲突 key=%s 既有指纹=%s 请求指纹=%s",
                        key, existing.fingerprint, fingerprint,
                    )
                    raise APIError(
                        ErrorCode.IDEMPOTENCY_REUSE,
                        "相同的 Idempotency-Key 已用于不同的请求内容",
                        details={"key": key},
                    )
                logger.info(
                    "幂等重放 key=%s 首次trace=%s status=%s",
                    key, existing.created_trace_id, existing.status_code,
                )
                return IdempotencyResult(existing, replayed=True)

            logger.debug("获得幂等锁，首次执行 key=%s", key)
            status_code, body = producer()
            self.execution_count += 1
            entry = IdempotencyEntry(
                key=key,
                fingerprint=fingerprint,
                status_code=status_code,
                body=body,
                created_trace_id=trace_id,
            )
            self._entries[key] = entry
            logger.info(
                "幂等结果已落盘 key=%s status=%s exec_count=%s",
                key, status_code, self.execution_count,
            )
            return IdempotencyResult(entry, replayed=False)

    def reset(self) -> None:
        with self._guard:
            self._entries.clear()
            self._key_locks.clear()
            self.execution_count = 0
