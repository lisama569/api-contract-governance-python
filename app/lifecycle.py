"""版本生命周期注册表：运行期可调整的弃用与下线状态机。

状态划分（按当前时间与配置动态推导，单一事实来源）::

    ACTIVE     正常服务，无任何弃用标记
    DEPRECATED 已打弃用标记但仍完整服务：读写行为、字段语义与弃用前完全一致，
               仅通过响应头（Deprecation/Sunset/Link）告知迁移建议
    SUNSET     到达下线时间（now >= sunset_at）：该版本所有请求被明确拒绝

一致性保证
--------
* 每个请求通过 ``snapshot()`` 拿到一份**不可变快照**，同一次请求内的
  拒绝判定与弃用响应头都来自同一份快照，不会因并发调整而自相矛盾；
* 状态调整（弃用标记 / 提前或延后下线时间）在 asyncio 锁内整体替换记录，
  正在执行的请求继续沿用其快照直至完成，已成功副作用不受影响；
* 生命周期状态与幂等/存储完全解耦：已固化的幂等记录与已落库数据
  不因状态调整而重复计算或丢失。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

logger = logging.getLogger("app")


class VersionStatus(str, Enum):
    """版本生命周期阶段（对外可机读）。"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"


def _parse_iso8601(raw: str) -> datetime:
    """解析带时区的 ISO 8601 时间串；非法输入抛 ValueError。"""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("时间必须携带时区（例如 2026-12-31T00:00:00+00:00）")
    return parsed


@dataclass(frozen=True)
class VersionLifecycle:
    """单个版本的生命周期快照（不可变，请求内一致性的载体）。"""

    version: int
    deprecated: bool = False
    successor_version: int | None = None
    sunset_at: str | None = None  # ISO 8601，到达即视为已下线

    def status(self, now: datetime) -> VersionStatus:
        if self.sunset_at is not None and now >= _parse_iso8601(self.sunset_at):
            return VersionStatus.SUNSET
        if self.deprecated:
            return VersionStatus.DEPRECATED
        return VersionStatus.ACTIVE

    def as_dict(self, now: datetime) -> dict:
        return {
            "version": self.version,
            "status": self.status(now).value,
            "deprecated": self.deprecated,
            "successor_version": self.successor_version,
            "sunset_at": self.sunset_at,
        }


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LifecycleRegistry:
    """进程内版本生命周期注册表（读无锁快照、写在 asyncio 锁内原子替换）。

    单事件循环下同步读取天然不会被写操作打断（写的关键区内不 await），
    因此 ``snapshot`` 是同步方法，供请求处理路径直接调用。
    """

    def __init__(self, versions: list[int] | tuple[int, ...], clock: Clock = _utc_now) -> None:
        self._records: dict[int, VersionLifecycle] = {
            v: VersionLifecycle(version=v) for v in versions
        }
        self._lock = asyncio.Lock()
        self._clock = clock

    def snapshot(self, version: int) -> VersionLifecycle | None:
        """返回指定版本当前的不可变快照；版本从未注册过则返回 None。"""
        return self._records.get(version)

    def status_of(self, version: int) -> VersionStatus | None:
        record = self._records.get(version)
        if record is None:
            return None
        return record.status(self._clock())

    def status_of_record(self, record: VersionLifecycle) -> VersionStatus:
        """对一份已取得的快照推导状态（保证同一请求内只用一份快照）。"""
        return record.status(self._clock())

    def describe(self, version: int) -> dict | None:
        """单个版本的当前状态描述；版本未注册返回 None。"""
        record = self._records.get(version)
        return record.as_dict(self._clock()) if record is not None else None

    def list_all(self) -> list[dict]:
        """所有已注册版本的当前状态（供状态查询接口）。"""
        now = self._clock()
        return [self._records[v].as_dict(now) for v in sorted(self._records)]

    async def deprecate(
        self, version: int, successor_version: int | None, sunset_at: str | None
    ) -> VersionLifecycle | None:
        """给仍在服务的版本打弃用标记；可同时给出建议迁移版本与计划下线时间。"""
        async with self._lock:
            current = self._records.get(version)
            if current is None:
                return None
            if sunset_at is not None:
                _parse_iso8601(sunset_at)  # 非法时间直接 ValueError，避免污染注册表
            updated = replace(
                current,
                deprecated=True,
                successor_version=successor_version,
                sunset_at=sunset_at if sunset_at is not None else current.sunset_at,
            )
            self._records[version] = updated
        logger.info(
            "event=version_deprecated version=%s successor=%s sunset_at=%s status=%s",
            version,
            successor_version,
            updated.sunset_at,
            updated.status(self._clock()).value,
        )
        return updated

    async def set_sunset(self, version: int, sunset_at: str | None) -> VersionLifecycle | None:
        """调整下线时间（可提前或延后；传 None 表示取消下线计划）。"""
        async with self._lock:
            current = self._records.get(version)
            if current is None:
                return None
            if sunset_at is not None:
                _parse_iso8601(sunset_at)
            updated = replace(current, sunset_at=sunset_at)
            self._records[version] = updated
        logger.info(
            "event=version_sunset_adjusted version=%s sunset_at=%s status=%s",
            version,
            sunset_at,
            updated.status(self._clock()).value,
        )
        return updated
