"""文档资源路由：同一套处理逻辑按契约版本装配为 /api/v1、/api/v2。

写操作（POST 创建 / PUT 替换）支持可选的 ``Idempotency-Key`` 请求头；
携带相同键的重复或并发请求只产生一次真实副作用，其余请求回放首次结果。
PUT 必须携带版本字段（v1: rev / v2: revision），用于过期写入检测。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from app import contracts
from app.errors import AppError, ErrorCode
from app.idempotency import IdempotencyStore, ReplayOutcome
from app.lifecycle import LifecycleRegistry, VersionLifecycle, VersionStatus
from app.store import DocumentStore
from app.tracing import DEPRECATION_HEADERS_SCOPE_KEY

logger = logging.getLogger("app")

IDEMPOTENCY_HEADER = "idempotency-key"
_IDEMPOTENCY_MAX_LEN = 128

WriteExecutor = Callable[[], Awaitable[tuple[int, dict]]]


def _fingerprint(method: str, path: str, raw_body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.encode())
    digest.update(b"\n")
    digest.update(path.encode())
    digest.update(b"\n")
    digest.update(raw_body)
    return digest.hexdigest()


def _validate_idempotency_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key or len(key) > _IDEMPOTENCY_MAX_LEN:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "Idempotency-Key 必须是非空且长度不超过 128 的字符串",
        )
    return key


async def _json_body(request: Request) -> tuple[dict[str, Any], bytes]:
    raw = await request.body()
    if not raw:
        raise AppError(ErrorCode.VALIDATION_ERROR, "请求体不能为空")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求体不是合法的 JSON",
            details={"reason": exc.msg},
        ) from exc
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.VALIDATION_ERROR, "请求体必须是 JSON 对象")
    return payload, raw


def _sunset_error(record: VersionLifecycle) -> AppError:
    """版本已下线的统一拒绝结论（410），details 给出可机读的判定依据。"""
    successor = record.successor_version or contracts.LATEST_VERSION
    return AppError(
        ErrorCode.VERSION_SUNSET,
        f"API 版本 v{record.version} 已于 {record.sunset_at} 下线，请迁移到 v{successor}",
        details={
            "requested_version": record.version,
            "status": VersionStatus.SUNSET.value,
            "sunset_at": record.sunset_at,
            "successor_version": successor,
            "latest_version": contracts.LATEST_VERSION,
        },
    )


def _deprecation_headers(record: VersionLifecycle) -> dict[str, str]:
    """弃用版本的告知响应头：已弃用、建议迁到哪、什么时候下线。"""
    headers = {"deprecation": "true"}
    if record.sunset_at is not None:
        sunset_dt = datetime.fromisoformat(record.sunset_at).astimezone(timezone.utc)
        headers["sunset"] = format_datetime(sunset_dt, usegmt=True)
    if record.successor_version is not None:
        headers["link"] = f'</api/v{record.successor_version}>; rel="successor-version"'
    return headers


def _enforce_lifecycle(
    request: Request, lifecycle: LifecycleRegistry, version: int
) -> None:
    """在请求处理入口做一次生命周期判定。

    整个请求只取一份不可变快照：拒绝判定与弃用响应头都来自它，
    运行期的并发状态调整不会让同一请求得出自相矛盾的结论。
    """
    record = lifecycle.snapshot(version)
    if record is None:  # 路由已注册但注册表无记录：装配错误，按不支持处理
        raise AppError(
            ErrorCode.VERSION_NOT_SUPPORTED,
            f"不支持的 API 版本 v{version}",
            details={"requested_version": version},
        )
    status = lifecycle.status_of_record(record)
    if status is VersionStatus.SUNSET:
        logger.info(
            "event=version_sunset_rejected version=%s sunset_at=%s",
            version,
            record.sunset_at,
        )
        raise _sunset_error(record)
    if status is VersionStatus.DEPRECATED:
        # 读写行为完全不变，仅附加告知头（成功与错误响应都会携带）
        request.scope[DEPRECATION_HEADERS_SCOPE_KEY] = _deprecation_headers(record)
        logger.info(
            "event=version_deprecated_serving version=%s successor=%s sunset_at=%s",
            version,
            record.successor_version,
            record.sunset_at,
        )


def create_router(
    version: int,
    store: DocumentStore,
    idempotency: IdempotencyStore,
    lifecycle: LifecycleRegistry,
) -> APIRouter:
    """为指定契约版本创建文档资源路由。"""
    router = APIRouter(prefix=f"/api/v{version}", tags=[f"v{version}"])

    async def _run_write(
        request: Request,
        raw_body: bytes,
        executor: Callable[[], Awaitable[tuple[int, dict]]],
    ) -> JSONResponse:
        """以幂等语义执行一次写入。

        返回值始终是 (状态码, 响应体)：执行权持有者固化结果，其它请求回放。
        """
        key = _validate_idempotency_key(request.headers.get(IDEMPOTENCY_HEADER))
        if key is None:
            status, body = await executor()
            return JSONResponse(status_code=status, content=body)

        fp = _fingerprint(request.method, request.url.path, raw_body)
        verdict = await idempotency.acquire(key, fp)

        if verdict is ReplayOutcome.CONFLICT:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同 Idempotency-Key 已用于不同的请求内容",
                details={"idempotency_key": key},
            )

        if verdict is ReplayOutcome.EXECUTE:
            try:
                status, body = await executor()
            except Exception:
                # 未固化结果前失败：释放占位，允许并发方/重试方重新执行
                await idempotency.release_in_flight(key)
                raise
            await idempotency.complete(key, status, body)
            logger.info(
                "event=write_executed idempotency_key=%s status=%s",
                key,
                status,
            )
            return JSONResponse(status_code=status, content=body)

        if verdict is ReplayOutcome.REPLAY:
            record = idempotency.get_record(key)
        else:  # IN_FLIGHT：首个并发请求尚未完成，等待其固化结果
            logger.info(
                "event=idempotency_wait_start idempotency_key=%s reason=首个请求执行中",
                key,
            )
            record = await idempotency.wait_for_completion(key)
            if record is None:
                raise AppError(
                    ErrorCode.STATE_CONFLICT,
                    "相同 Idempotency-Key 的首个请求仍在处理，请稍后重试",
                    details={"idempotency_key": key},
                )
            logger.info(
                "event=idempotency_wait_end idempotency_key=%s outcome=REPLAY",
                key,
            )

        logger.info(
            "event=write_replayed idempotency_key=%s status=%s",
            key,
            record.status_code,
        )
        return JSONResponse(status_code=record.status_code, content=record.body)

    @router.post("/documents")
    async def create_document(request: Request) -> JSONResponse:
        _enforce_lifecycle(request, lifecycle, version)
        payload, raw = await _json_body(request)
        # id 是资源定位字段，独立于各版本的内容契约，先取出再按版本解析其余字段
        document_id = payload.get("id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "请求体必须包含非空字符串字段 id",
                details={"field": "id"},
            )
        contracts.validate_document_id(document_id)
        content_payload = {k: v for k, v in payload.items() if k != "id"}
        fields = contracts.deserialize_fields(content_payload, version)
        logger.info(
            "event=create_request version=%s id=%s input=%s",
            version,
            document_id,
            payload,
        )

        async def execute() -> tuple[int, dict]:
            document = await store.create(document_id, fields)
            return 201, contracts.serialize_document(document, version)

        return await _run_write(request, raw, execute)

    @router.get("/documents/{document_id}")
    async def get_document(document_id: str, request: Request) -> JSONResponse:
        _enforce_lifecycle(request, lifecycle, version)
        contracts.validate_document_id(document_id)
        document = await store.get(document_id)
        if document is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"文档 {document_id} 不存在",
                details={"document_id": document_id},
            )
        return JSONResponse(
            status_code=200,
            content=contracts.serialize_document(document, version),
        )

    @router.put("/documents/{document_id}")
    async def replace_document(document_id: str, request: Request) -> JSONResponse:
        _enforce_lifecycle(request, lifecycle, version)
        contracts.validate_document_id(document_id)
        payload, raw = await _json_body(request)
        expected = contracts.expected_revision_from(payload, version)
        # 版本字段是并发前提而非内容，剔除后按版本契约解析内容字段
        version_field = "rev" if version == 1 else "revision"
        content_payload = {k: v for k, v in payload.items() if k != version_field}
        fields = contracts.deserialize_fields(content_payload, version)
        logger.info(
            "event=replace_request version=%s id=%s expected_revision=%s input=%s",
            version,
            document_id,
            expected,
            payload,
        )

        async def execute() -> tuple[int, dict]:
            document = await store.replace(document_id, fields, expected)
            return 200, contracts.serialize_document(document, version)

        return await _run_write(request, raw, execute)

    return router


def _path_matches_other_route(request: Request) -> bool:
    """该路径是否匹配某个已注册路由、只是方法不允许（PARTIAL 匹配）。"""
    for route in request.app.routes:
        if "{raw_version}" in getattr(route, "path", ""):
            continue  # 跳过兜底路由自身
        match, _ = route.matches(request.scope)
        if match is Match.PARTIAL:
            return True
    return False


def create_unsupported_version_router(lifecycle: LifecycleRegistry) -> APIRouter:
    """/api 下未匹配到具体路由的兜底：按真实原因给出可区分结论。

    判定顺序（每种结论都落入统一错误结构）：
    版本号写法非法 -> VALIDATION_ERROR；
    版本已下线 -> VERSION_SUNSET；
    版本从未支持 -> VERSION_NOT_SUPPORTED；
    版本在服务但路径不存在 -> NOT_FOUND；
    路径存在但方法不支持 -> METHOD_NOT_ALLOWED。
    """
    router = APIRouter()

    @router.api_route(
        "/api/v{raw_version}/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unsupported_version(request: Request, raw_version: str, rest: str) -> JSONResponse:
        return _reject(request, raw_version)

    @router.api_route(
        "/api/v{raw_version}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unsupported_version_collection(request: Request, raw_version: str) -> JSONResponse:
        return _reject(request, raw_version)

    def _reject(request: Request, raw_version: str) -> JSONResponse:
        version = contracts.parse_version(raw_version)
        record = lifecycle.snapshot(version)
        if record is not None and lifecycle.status_of_record(record) is VersionStatus.SUNSET:
            logger.info(
                "event=version_sunset_rejected version=%s sunset_at=%s",
                version,
                record.sunset_at,
            )
            raise _sunset_error(record)
        if record is None:
            raise AppError(
                ErrorCode.VERSION_NOT_SUPPORTED,
                f"不支持的 API 版本 v{version}，请使用 v{min(contracts.SUPPORTED_VERSIONS)} - v{max(contracts.SUPPORTED_VERSIONS)}",
                details={
                    "requested_version": version,
                    "reason": "never_supported",
                    "supported_versions": list(contracts.SUPPORTED_VERSIONS),
                    "latest_version": contracts.LATEST_VERSION,
                },
            )
        # 版本仍在服务却落到兜底：路径不存在，或路径存在但方法不支持
        if _path_matches_other_route(request):
            raise AppError(
                ErrorCode.METHOD_NOT_ALLOWED,
                f"v{version} 的该资源不支持 {request.method} 方法",
                details={
                    "method": request.method,
                    "path": request.url.path,
                    "version": version,
                },
            )
        raise AppError(
            ErrorCode.NOT_FOUND,
            "请求的路径不存在",
            details={
                "method": request.method,
                "path": request.url.path,
                "version": version,
            },
        )

    return router


def create_admin_router(lifecycle: LifecycleRegistry) -> APIRouter:
    """版本生命周期的运行期管理：状态查询与弃用/下线调整。"""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/versions")
    async def list_versions() -> dict:
        """每个已注册版本当前的状态、弃用标记与下线时间。"""
        return {"versions": lifecycle.list_all()}

    @router.put("/versions/{raw_version}/lifecycle")
    async def update_lifecycle(raw_version: str, request: Request) -> dict:
        """调整版本生命周期：打弃用标记、设置/提前/延后/取消下线时间。

        请求体字段（均可选）：deprecated(bool)、successor_version(int)、sunset_at(ISO 8601 或 null)。
        """
        version = contracts.parse_version(raw_version)
        if lifecycle.snapshot(version) is None:
            raise AppError(
                ErrorCode.VERSION_NOT_SUPPORTED,
                f"不支持的 API 版本 v{version}，无法调整其生命周期",
                details={
                    "requested_version": version,
                    "supported_versions": list(contracts.SUPPORTED_VERSIONS),
                },
            )
        payload, _ = await _json_body(request)
        allowed = {"deprecated", "successor_version", "sunset_at"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"生命周期调整不支持字段 {unknown}",
                details={"unknown_fields": unknown, "supported_fields": sorted(allowed)},
            )

        logger.info(
            "event=lifecycle_adjust_request version=%s input=%s",
            version,
            payload,
        )
        try:
            if payload.get("deprecated") is True:
                successor = payload.get("successor_version")
                if successor is not None:
                    if not isinstance(successor, int) or lifecycle.snapshot(successor) is None:
                        raise AppError(
                            ErrorCode.VALIDATION_ERROR,
                            "successor_version 必须是已注册的版本号",
                            details={"successor_version": successor},
                        )
                await lifecycle.deprecate(version, successor, payload.get("sunset_at"))
            elif "sunset_at" in payload:
                # 未同时打弃用标记时，单独调整下线时间（提前/延后/取消）
                await lifecycle.set_sunset(version, payload["sunset_at"])
        except ValueError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"sunset_at 不是合法的 ISO 8601 时间: {exc}",
                details={"sunset_at": payload.get("sunset_at")},
            ) from exc

        return lifecycle.describe(version)

    return router
