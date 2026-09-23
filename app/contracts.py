"""多版本接口契约：版本协商与按版本字段编解码。

内部资源使用规范字段：``title/body/owner/tags/revision/created_at/updated_at``。

版本契约
--------
v1（旧）:
    请求/响应字段 = title, content, created_by, rev
    - 不感知 tags / created_at / updated_at
v2（新）:
    请求/响应字段 = title, body, owner, tags, revision, created_at, updated_at
    - content -> body, created_by -> owner, rev -> revision（字段改名）
    - 新增 tags / created_at / updated_at

保证
----
* 新增字段只出现在新版本：v1 响应永远不包含 tags 等新字段，旧语义不变；
* 字段改名后，旧版本只能用旧字段名，向 v1 传 body/owner 会被判为未知字段并拒绝，
  不会静默映射到错位数据；
* 不受支持的版本在路由层以 VERSION_NOT_SUPPORTED 明确拒绝。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.errors import AppError, ErrorCode

SUPPORTED_VERSIONS: tuple[int, ...] = (1, 2)
LATEST_VERSION: int = 2

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# v1 -> 内部规范名
_V1_FIELD_MAP = {
    "title": "title",
    "content": "body",
    "created_by": "owner",
}

# 每个版本允许出现在请求体中的字段
_REQUEST_FIELDS: dict[int, frozenset[str]] = {
    1: frozenset({"title", "content", "created_by"}),
    2: frozenset({"title", "body", "owner", "tags"}),
}


class _FieldsV1(BaseModel):
    title: str
    content: str
    created_by: str


class _FieldsV2(BaseModel):
    title: str
    body: str
    owner: str
    tags: list[str] = []


_MODELS: dict[int, type[BaseModel]] = {1: _FieldsV1, 2: _FieldsV2}


def parse_version(raw: str | None) -> int:
    """将路径中的版本段解析为整数版本号，非法格式抛 VALIDATION_ERROR。"""
    if raw is None or not raw.isdigit():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法解析的 API 版本: {raw!r}，版本段必须是数字，例如 v{LATEST_VERSION}",
            details={"received_version": raw, "supported": list(SUPPORTED_VERSIONS)},
        )
    return int(raw)


def is_supported(version: int) -> bool:
    return version in SUPPORTED_VERSIONS


def validate_document_id(document_id: str) -> None:
    if not _ID_PATTERN.match(document_id):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文档 ID 只能包含字母、数字、下划线与连字符，长度 1-64",
            details={"document_id": document_id},
        )


def deserialize_fields(payload: dict[str, Any], version: int) -> dict[str, Any]:
    """按版本解析写入入参，映射为内部规范字段；未知字段与类型错误一律拒绝。"""
    if not isinstance(payload, dict):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求体必须是 JSON 对象",
        )
    allowed = _REQUEST_FIELDS[version]
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"v{version} 契约不包含字段 {unknown}，请使用该版本支持的字段名",
            details={
                "version": version,
                "unknown_fields": unknown,
                "supported_fields": sorted(allowed),
            },
        )
    try:
        model = _MODELS[version](**payload)
    except ValidationError as exc:
        field_errors = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
            details={"version": version, "errors": field_errors},
        ) from exc

    data = model.model_dump()
    if version == 1:
        return {
            "title": data["title"],
            "body": data["content"],
            "owner": data["created_by"],
            "tags": [],
        }
    return data


def serialize_document(document: Any, version: int) -> dict[str, Any]:
    """按版本序列化资源；v1 严格使用旧字段名且不得泄漏 v2 新字段。"""
    if version == 1:
        return {
            "id": document.id,
            "title": document.title,
            "content": document.body,
            "created_by": document.owner,
            "rev": document.revision,
        }
    return {
        "id": document.id,
        "title": document.title,
        "body": document.body,
        "owner": document.owner,
        "tags": list(document.tags),
        "revision": document.revision,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def expected_revision_from(payload: dict[str, Any], version: int) -> int:
    """从请求体读取调用方期望基于的 revision（用于更新冲突检测）。"""
    field_name = "rev" if version == 1 else "revision"
    raw = payload.get(field_name)
    if raw is None:
        raise AppError(
            ErrorCode.PRECONDITION_REQUIRED,
            f"更新必须携带当前资源版本字段 {field_name!r}（乐观并发前提）",
            details={"required_field": field_name},
        )
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"{field_name!r} 必须是非负整数",
            details={"field": field_name, "received": raw},
        )
    return raw
