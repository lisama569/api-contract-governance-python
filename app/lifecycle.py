"""版本生命周期治理：ACTIVE -> DEPRECATED -> SUNSET 的运行期状态机。

状态划分
--------
ACTIVE:
    正常服务，无任何弃用信号。
DEPRECATED:
    仍在服务，读写行为与字段语义和弃用前完全一致；
    响应额外携带 Deprecation / Sunset / Link 头，提示迁移目标与计划下线时间。
    ``sunset_at`` 到期后按 SUNSET 处理（自动下线）。
SUNSET:
    已下线，该版本下所有请求以 VERSION_SUNSET（410）明确拒绝。

一致性保证
----------
* 每个请求在进入时通过 ``VersionRegistry.snapshot`` 取一次原子快照，
  整个请求生命周期内只依据该快照判定，运行期调整不会让单个请求看到自相矛盾的状态；
* 生命周期调整只影响后续请求的快照，已完成的副作用（文档写入、幂等记录）不受影响。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from enum import Enum
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import AppError, ErrorCode, error_body, http_status_for
from app.tracing import SCOPE_KEY as TRACE_SCOPE_KEY
from app.tracing import TRACE_HEADER

logger = logging.getLogger("app")

LIFECYCLE_SCOPE_KEY = "app.version_lifecycle"

# 响应头（RFC 9745 / RFC 8594 / RFC 8288）
DEPRECATION_HEADER = "deprecation"
SUNSET_HEADER = "sunset"
LINK_HEADER = "link"

_VERSION_PREFIX = re.compile(r"^/api/v([^/]+)(?:/|$)")


class VersionState(str, Enum):
    """版本生命周期状态。"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_instant(raw: str, *, field: str = "sunset_at") -> datetime:
    """解析 ISO 8601 时间点（支持 Z 后缀）；非法输入抛 VALIDATION_ERROR。"""
    if not isinstance(raw, str) or not raw.strip():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"{field} 必须是非空 ISO 8601 时间字符串",
            details={"field": field, "received": raw},
        )
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"{field} 不是合法的 ISO 8601 时间: {raw!r}",
            details={"field": field, "received": raw},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class VersionLifecycle:
    """某个版本在某一时刻的不可变生命周期快照。"""

    version: int
    state: VersionState
    successor: int | None = None
    deprecated_at: str | None = None
    sunset_at: str | None = None

    def effective_state(self, now: datetime) -> VersionState:
        """考虑 sunset_at 到期后的实际状态（弃用期到期即视为已下线）。"""
        if self.state is VersionState.DEPRECATED and self.sunset_at is not None:
            if parse_instant(self.sunset_at) <= now:
                return VersionState.SUNSET
        return self.state

    def to_dict(self, now: datetime) -> dict:
        """序列化为对外可查询的状态视图。"""
        return {
            "version": self.version,
            "state": self.effective_state(now).value,
            "successor": self.successor,
            "deprecated_at": self.deprecated_at,
            "sunset_at": self.sunset_at,
        }

    def deprecation_headers(self) -> dict[str, str]:
        """弃用期响应头：迁移目标与计划下线时间（不改变响应体）。"""
        headers: dict[str, str] = {}
        if self.deprecated_at is not None:
            ts = int(parse_instant(self.deprecated_at, field="deprecated_at").timestamp())
            headers[DEPRECATION_HEADER] = f"@{ts}"
        else:
            headers[DEPRECATION_HEADER] = "true"
        if self.sunset_at is not None:
            headers[SUNSET_HEADER] = format_datetime(
                parse_instant(self.sunset_at), usegmt=True
            )
        if self.successor is not None:
            headers[LINK_HEADER] = (
                f'</api/v{self.successor}/documents>; rel="successor-version"'
            )
        return headers


class VersionRegistry:
    """运行期可调整的版本生命周期注册表（单事件循环，asyncio 锁保证快照原子性）。"""

    def __init__(self, versions: tuple[int, ...], latest: int) -> None:
        self._latest = latest
        self._states: dict[int, VersionLifecycle] = {
            v: VersionLifecycle(version=v, state=VersionState.ACTIVE) for v in versions
        }
        self._lock = asyncio.Lock()

    @property
    def latest(self) -> int:
        return self._latest

    def _known_or_raise(self, version: int) -> None:
        if version not in self._states:
            raise AppError(
                ErrorCode.VERSION_NOT_SUPPORTED,
                f"未知的 API 版本 v{version}",
                details={
                    "requested_version": version,
                    "known_versions": sorted(self._states),
                },
            )

    async def snapshot(self, version: int) -> VersionLifecycle | None:
        """取指定版本的原子快照；未知版本返回 None。"""
        async with self._lock:
            return self._states.get(version)

    async def all_snapshots(self) -> list[VersionLifecycle]:
        """取全部已知版本的快照（按版本号升序）。"""
        async with self._lock:
            return [self._states[v] for v in sorted(self._states)]

    async def deprecate(self, version: int, successor: int, sunset_at: str) -> VersionLifecycle:
        """把仍在服务的版本标记为弃用，指定迁移目标与计划下线时间。"""
        async with self._lock:
            self._known_or_raise(version)
            self._known_or_raise(successor)
            if successor == version:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "迁移目标版本不能是被弃用版本自身",
                    details={"version": version, "successor": successor},
                )
            parsed = parse_instant(sunset_at)
            current = self._states[version]
            updated = VersionLifecycle(
                version=version,
                state=VersionState.DEPRECATED,
                successor=successor,
                deprecated_at=current.deprecated_at or _iso(_utcnow()),
                sunset_at=_iso(parsed),
            )
            self._states[version] = updated
        logger.info(
            "event=version_deprecated version=%s successor=%s sunset_at=%s",
            version,
            successor,
            updated.sunset_at,
        )
        return updated

    async def activate(self, version: int) -> VersionLifecycle:
        """撤销弃用/下线，恢复正常服务。"""
        async with self._lock:
            self._known_or_raise(version)
            updated = VersionLifecycle(version=version, state=VersionState.ACTIVE)
            self._states[version] = updated
        logger.info("event=version_activated version=%s", version)
        return updated

    async def set_sunset(self, version: int, sunset_at: str) -> VersionLifecycle:
        """调整计划下线时间（提前或延后）；仅对弃用中的版本有意义。"""
        async with self._lock:
            self._known_or_raise(version)
            current = self._states[version]
            if current.state is not VersionState.DEPRECATED:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    f"版本 v{version} 当前未处于弃用期，无法调整下线时间",
                    details={"version": version, "state": current.state.value},
                )
            parsed = parse_instant(sunset_at)
            updated = VersionLifecycle(
                version=version,
                state=VersionState.DEPRECATED,
                successor=current.successor,
                deprecated_at=current.deprecated_at,
                sunset_at=_iso(parsed),
            )
            self._states[version] = updated
        logger.info(
            "event=version_sunset_rescheduled version=%s sunset_at=%s",
            version,
            updated.sunset_at,
        )
        return updated

    async def retire(self, version: int) -> VersionLifecycle:
        """立即下线该版本（保留迁移目标与下线时间供错误响应引用）。"""
        async with self._lock:
            self._known_or_raise(version)
            current = self._states[version]
            updated = VersionLifecycle(
                version=version,
                state=VersionState.SUNSET,
                successor=current.successor,
                deprecated_at=current.deprecated_at,
                sunset_at=current.sunset_at or _iso(_utcnow()),
            )
            self._states[version] = updated
        logger.info("event=version_retired version=%s", version)
        return updated


class VersionLifecycleMiddleware:
    """按请求原子快照执行生命周期判定。

    - 已下线版本：直接以 VERSION_SUNSET（410）拒绝，不进入路由；
    - 弃用中版本：放行并在响应上附加 Deprecation / Sunset / Link 头；
    - 其它情况（版本段非法、版本未知、非 /api/v* 路径）：放行给后续路由判定。
    """

    def __init__(self, app: ASGIApp, registry: VersionRegistry) -> None:  # type: ignore[assignment]
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        match = _VERSION_PREFIX.match(path)
        snapshot: VersionLifecycle | None = None
        if match is not None and match.group(1).isdigit():
            snapshot = await self.registry.snapshot(int(match.group(1)))

        if snapshot is None:
            await self.app(scope, receive, send)
            return

        now = _utcnow()
        effective = snapshot.effective_state(now)
        logger.info(
            "event=version_lifecycle_check version=%s state=%s sunset_at=%s successor=%s",
            snapshot.version,
            effective.value,
            snapshot.sunset_at,
            snapshot.successor,
        )

        if effective is VersionState.SUNSET:
            trace_id = scope.get(TRACE_SCOPE_KEY)
            body = error_body(
                ErrorCode.VERSION_SUNSET,
                f"API 版本 v{snapshot.version} 已下线，请迁移到 v{snapshot.successor}"
                if snapshot.successor is not None
                else f"API 版本 v{snapshot.version} 已下线",
                {
                    "requested_version": snapshot.version,
                    "successor": snapshot.successor,
                    "sunset_at": snapshot.sunset_at,
                },
                trace_id,
            )
            headers = []
            if trace_id:
                headers.append((TRACE_HEADER.encode(), str(trace_id).encode()))
            await send(
                {
                    "type": "http.response.start",
                    "status": http_status_for(ErrorCode.VERSION_SUNSET),
                    "headers": [(b"content-type", b"application/json"), *headers],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(body, ensure_ascii=False).encode(),
                }
            )
            return

        if effective is VersionState.DEPRECATED:
            deprecation_headers = snapshot.deprecation_headers()

            async def send_with_deprecation(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    raw_headers = message.setdefault("headers", [])
                    existing = {k.lower() for k, _ in raw_headers}
                    for name, value in deprecation_headers.items():
                        if name.encode() not in existing:
                            raw_headers.append((name.encode(), value.encode()))
                await send(message)

            await self.app(scope, receive, send_with_deprecation)
            return

        await self.app(scope, receive, send)
