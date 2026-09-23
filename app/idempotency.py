"""幂等键存储：重复与并发请求只放行一次真实副作用。

状态机（同一 Idempotency-Key）::

    首个请求:  acquire -> EXECUTE
       成功 -> complete(record)          -> 后来者 REPLAY（回放同一状态码+响应体）
       失败 -> release_in_flight(key)    -> 后来者 EXECUTE（可重试，不会产生半截结果）
    并发请求:  acquire -> IN_FLIGHT      -> wait_for_completion 后 REPLAY
    同键不同体: acquire -> CONFLICT      -> 409 IDEMPOTENCY_CONFLICT

实现基于 asyncio 原语，保证同一事件循环内的真正并发请求不会互相阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("app")


class ReplayOutcome(str, Enum):
    EXECUTE = "EXECUTE"      # 获得首次执行权
    REPLAY = "REPLAY"        # 命中已完成结果，直接回放
    IN_FLIGHT = "IN_FLIGHT"  # 首个请求仍在执行（等待后再判定）
    CONFLICT = "CONFLICT"    # 同键不同请求体


@dataclass
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    status_code: int = 0
    body: dict = field(default_factory=dict)
    completed: bool = False


@dataclass
class _Slot:
    record: IdempotencyRecord
    in_flight: bool = True
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class IdempotencyStore:
    """进程内幂等记录存储（基于 asyncio，适用于单进程事件循环）。"""

    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, request_fingerprint: str) -> ReplayOutcome:
        """登记一次带幂等键的请求并给出判定结果。"""
        async with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                self._slots[key] = _Slot(
                    record=IdempotencyRecord(key=key, request_fingerprint=request_fingerprint)
                )
                logger.info(
                    "event=idempotency_acquire key=%s verdict=EXECUTE fingerprint=%s",
                    key,
                    request_fingerprint,
                )
                return ReplayOutcome.EXECUTE
            if slot.record.request_fingerprint != request_fingerprint:
                logger.info(
                    "event=idempotency_acquire key=%s verdict=CONFLICT fingerprint=%s expected=%s",
                    key,
                    request_fingerprint,
                    slot.record.request_fingerprint,
                )
                return ReplayOutcome.CONFLICT
            if not slot.in_flight:
                logger.info("event=idempotency_acquire key=%s verdict=REPLAY", key)
                return ReplayOutcome.REPLAY
            logger.info("event=idempotency_acquire key=%s verdict=IN_FLIGHT", key)
            return ReplayOutcome.IN_FLIGHT

    def get_record(self, key: str) -> IdempotencyRecord | None:
        """返回指定键当前记录（用于 REPLAY 判定）。"""
        slot = self._slots.get(key)
        return slot.record if slot is not None else None

    async def wait_for_completion(self, key: str, timeout: float = 5.0) -> IdempotencyRecord | None:
        """供拿到 IN_FLIGHT 的请求等待首个请求完成；返回已完成记录，超时则返回 None。"""
        slot = self._slots.get(key)
        if slot is None:
            return None
        try:
            await asyncio.wait_for(slot.finished.wait(), timeout=timeout)
        except TimeoutError:
            logger.info("event=idempotency_wait key=%s verdict=TIMEOUT", key)
            return None
        if not slot.in_flight and slot.record.completed:
            logger.info("event=idempotency_wait key=%s verdict=REPLAY", key)
            return slot.record
        return None

    async def complete(self, key: str, status_code: int, body: dict) -> None:
        """首个请求成功完成，固化结果供后续回放。"""
        async with self._lock:
            slot = self._slots[key]
            slot.record.status_code = status_code
            slot.record.body = body
            slot.record.completed = True
            slot.in_flight = False
            slot.finished.set()
        logger.info("event=idempotency_complete key=%s status=%s", key, status_code)

    async def release_in_flight(self, key: str) -> None:
        """首个请求在固化结果前失败：移除占位并唤醒等待者，允许其它请求重新获得执行权。"""
        async with self._lock:
            slot = self._slots.pop(key, None)
        if slot is not None:
            slot.finished.set()
        logger.info("event=idempotency_release key=%s", key)
