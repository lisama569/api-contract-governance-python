"""业务编排：版本协商后的载荷解析、幂等执行、乐观并发、错误翻译。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.contracts import (
    IncompatibleField,
    NoteRecord,
    parse_create,
    parse_update,
    render,
)
from app.errors import APIError, ErrorCode
from app.idempotency import IdempotencyStore
from app.logging_setup import get_logger
from pydantic import ValidationError
from app.repository import (
    ConcurrentUpdateError,
    NoteRepository,
    ResourceNotFoundError,
)
from app.tracing import get_trace_id
from app.versions import VersionInfo

logger = get_logger("contract.service")


def _fingerprint(parts: list[Any]) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _not_found(note_id: str) -> APIError:
    return APIError(ErrorCode.NOT_FOUND, details={"resource": "note", "id": note_id})


def _validation_error(exc: ValidationError) -> APIError:
    fields = [
        {"field": ".".join(str(p) for p in err.get("loc", [])),
         "issue": err.get("type", "invalid")}
        for err in exc.errors()
    ]
    return APIError(
        ErrorCode.VALIDATION_ERROR,
        "请求参数校验失败",
        details={"fields": fields},
    )


class NoteService:
    def __init__(self, repo: NoteRepository, idempotency: IdempotencyStore) -> None:
        self.repo = repo
        self.idempotency = idempotency

    # ---- create ----
    def create_note(
        self, version: VersionInfo, payload: dict[str, Any], idempotency_key: str | None
    ) -> tuple[int, dict[str, Any], bool]:
        """返回 (status_code, body, replayed)。"""
        logger.info("创建请求 version=%s 输入=%s key=%s", version.name, payload, idempotency_key)
        try:
            fields = parse_create(version, payload)
        except IncompatibleField as exc:
            raise APIError(
                ErrorCode.VERSION_INCOMPATIBLE,
                f"字段 '{exc.field}' 与协商版本 {version.name} 不兼容：{exc.reason}",
                details={"version": version.name, "field": exc.field, "reason": exc.reason},
            ) from None
        except ValidationError as exc:
            raise _validation_error(exc) from None

        def producer() -> tuple[int, dict[str, Any]]:
            record = self.repo.create(**fields)
            logger.debug("真实写库 create id=%s revision=1", record.id)
            return 201, render(version, record)

        if idempotency_key is None:
            status, body = producer()
            return status, body, False

        fp = _fingerprint(["create", version.name, fields])
        result = self.idempotency.run_once(idempotency_key, fp, producer, get_trace_id() or "-")
        body = result.entry.body
        logger.info(
            "创建判定 key=%s 依据=%s status=%s",
            idempotency_key,
            "命中首次结果重放" if result.replayed else "首次执行",
            result.entry.status_code,
        )
        return result.entry.status_code, body, result.replayed

    # ---- read ----
    def get_note(self, version: VersionInfo, note_id: str) -> dict[str, Any]:
        logger.info("读取请求 version=%s id=%s", version.name, note_id)
        try:
            record = self.repo.get(note_id)
        except ResourceNotFoundError:
            raise _not_found(note_id) from None
        return render(version, record)

    # ---- update ----
    def update_note(
        self,
        version: VersionInfo,
        note_id: str,
        payload: dict[str, Any],
        expected_revision: int,
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, Any], bool]:
        logger.info(
            "更新请求 version=%s id=%s if-match=%s 输入=%s key=%s",
            version.name, note_id, expected_revision, payload, idempotency_key,
        )
        try:
            changes = parse_update(version, payload)
        except IncompatibleField as exc:
            raise APIError(
                ErrorCode.VERSION_INCOMPATIBLE,
                f"字段 '{exc.field}' 与协商版本 {version.name} 不兼容：{exc.reason}",
                details={"version": version.name, "field": exc.field, "reason": exc.reason},
            ) from None
        except ValidationError as exc:
            raise _validation_error(exc) from None

        def producer() -> tuple[int, dict[str, Any]]:
            try:
                record: NoteRecord = self.repo.update(note_id, expected_revision, changes)
            except ResourceNotFoundError:
                raise _not_found(note_id) from None
            except ConcurrentUpdateError as exc:
                logger.info(
                    "陈旧写入判定 id=%s 依据=期望revision=%s 当前revision=%s",
                    note_id, exc.expected, exc.actual,
                )
                raise APIError(
                    ErrorCode.PRECONDITION_FAILED,
                    f"更新基于陈旧版本（期望 {exc.expected}，实际 {exc.actual}），已拒绝以防覆盖写入",
                    details={
                        "id": note_id,
                        "expected_revision": exc.expected,
                        "current_revision": exc.actual,
                    },
                ) from None
            logger.debug("真实写库 update id=%s revision=%s", record.id, record.revision)
            return 200, render(version, record)

        if idempotency_key is None:
            status, body = producer()
            return status, body, False

        fp = _fingerprint(["update", version.name, note_id, expected_revision, changes])
        result = self.idempotency.run_once(idempotency_key, fp, producer, get_trace_id() or "-")
        logger.info(
            "更新判定 key=%s 依据=%s status=%s",
            idempotency_key,
            "命中首次结果重放" if result.replayed else "首次执行",
            result.entry.status_code,
        )
        return result.entry.status_code, result.entry.body, result.replayed

    # ---- delete ----
    def delete_note(
        self, note_id: str, expected_revision: int, idempotency_key: str | None
    ) -> tuple[int, bool]:
        logger.info(
            "删除请求 id=%s if-match=%s key=%s", note_id, expected_revision, idempotency_key
        )

        def producer() -> tuple[int, Any]:
            try:
                self.repo.delete(note_id, expected_revision)
            except ResourceNotFoundError:
                raise _not_found(note_id) from None
            except ConcurrentUpdateError as exc:
                logger.info(
                    "陈旧删除判定 id=%s 依据=期望revision=%s 当前revision=%s",
                    note_id, exc.expected, exc.actual,
                )
                raise APIError(
                    ErrorCode.PRECONDITION_FAILED,
                    f"删除基于陈旧版本（期望 {exc.expected}，实际 {exc.actual}），已拒绝",
                    details={
                        "id": note_id,
                        "expected_revision": exc.expected,
                        "current_revision": exc.actual,
                    },
                ) from None
            logger.debug("真实写库 delete id=%s", note_id)
            return 204, None

        if idempotency_key is None:
            status, _ = producer()
            return status, False
        fp = _fingerprint(["delete", note_id, expected_revision])
        result = self.idempotency.run_once(idempotency_key, fp, producer, get_trace_id() or "-")
        logger.info(
            "删除判定 key=%s 依据=%s status=%s",
            idempotency_key,
            "命中首次结果重放" if result.replayed else "首次执行",
            result.entry.status_code,
        )
        return result.entry.status_code, result.replayed
