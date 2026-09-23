"""文档资源路由：同一套处理逻辑按契约版本装配为 /api/v1、/api/v2。

写操作（POST 创建 / PUT 替换）支持可选的 ``Idempotency-Key`` 请求头；
携带相同键的重复或并发请求只产生一次真实副作用，其余请求回放首次结果。
PUT 必须携带版本字段（v1: rev / v2: revision），用于过期写入检测。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from app import contracts
from app.errors import AppError, ErrorCode
from app.idempotency import IdempotencyStore, ReplayOutcome
from app.store import DocumentStore

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


def create_router(
    version: int,
    store: DocumentStore,
    idempotency: IdempotencyStore,
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
    async def get_document(document_id: str) -> JSONResponse:
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


def create_unsupported_version_router() -> APIRouter:
    """/api 下未受支持版本的兜底：明确拒绝并给出可区分原因（而非 404）。"""
    router = APIRouter()

    @router.api_route(
        "/api/v{raw_version}/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unsupported_version(raw_version: str, rest: str) -> JSONResponse:
        return _reject(raw_version)

    @router.api_route(
        "/api/v{raw_version}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unsupported_version_collection(raw_version: str) -> JSONResponse:
        return _reject(raw_version)

    def _reject(raw_version: str) -> JSONResponse:
        version = contracts.parse_version(raw_version)
        raise AppError(
            ErrorCode.VERSION_NOT_SUPPORTED,
            f"不支持的 API 版本 v{version}，请使用 v{min(contracts.SUPPORTED_VERSIONS)} - v{max(contracts.SUPPORTED_VERSIONS)}",
            details={
                "requested_version": version,
                "supported_versions": list(contracts.SUPPORTED_VERSIONS),
                "latest_version": contracts.LATEST_VERSION,
            },
        )

    return router
