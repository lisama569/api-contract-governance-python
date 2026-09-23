"""各版本请求/响应契约视图与字段级版本兼容性判定。

契约演进规则：
  - v1 -> v2：body 改名为 content（语义移动，非新增），新增 tags。
  - 旧版本请求只能使用旧字段名；携带新版本字段（content/tags 打到 v1）
    会被明确拒绝（VERSION_INCOMPATIBLE），绝不静默丢弃或错配到其它字段。
  - 旧版本响应严格按旧视图投影，不会因新版本新增字段而改变语义。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.versions import VersionInfo


class NoteRecord(BaseModel):
    """规范资源模型（内部存储形态，不直接对外）。"""

    id: str
    title: str
    body: str
    tags: list[str] = []
    revision: int = 0


# ---- v1 契约（旧版本：字段名 body） ----
class NoteViewV1(BaseModel):
    id: str
    title: str
    body: str
    revision: int


class NoteCreateV1(BaseModel):
    title: str
    body: str


class NoteUpdateV1(BaseModel):
    title: str | None = None
    body: str | None = None


# ---- v2 契约（新版本：body 改名为 content，新增 tags） ----
class NoteViewV2(BaseModel):
    id: str
    title: str
    content: str
    tags: list[str]
    revision: int


class NoteCreateV2(BaseModel):
    title: str
    content: str
    tags: list[str] = []


class NoteUpdateV2(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None


_CREATE_MODELS = {1: NoteCreateV1, 2: NoteCreateV2}
_UPDATE_MODELS = {1: NoteUpdateV1, 2: NoteUpdateV2}

# 各版本允许出现的请求字段（白名单；其余字段一律拒绝）
_ALLOWED_CREATE_FIELDS = {1: {"title", "body"}, 2: {"title", "content", "tags"}}
_ALLOWED_UPDATE_FIELDS = {
    1: {"title", "body"},
    2: {"title", "content", "tags"},
}


class IncompatibleField(Exception):
    """请求载荷包含当前协商版本不认识/不允许的字段。"""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _reject_unknown(version: VersionInfo, payload: dict[str, Any], allowed: set[str]) -> None:
    for key in payload:
        if key not in allowed:
            raise IncompatibleField(
                key,
                f"字段在接口版本 {version.name} 中不存在（可能来自更新版本，存在数据错配风险）",
            )


def parse_create(version: VersionInfo, payload: dict[str, Any]) -> dict[str, Any]:
    """按版本解析创建载荷；返回规范字段字典 {title, body, tags}。"""
    allowed = _ALLOWED_CREATE_FIELDS[version.number]
    _reject_unknown(version, payload, allowed)
    model = _CREATE_MODELS[version.number](**payload)
    if version.number == 1:
        return {"title": model.title, "body": model.body, "tags": []}
    return {"title": model.title, "body": model.content, "tags": model.tags}


def parse_update(version: VersionInfo, payload: dict[str, Any]) -> dict[str, Any]:
    """按版本解析更新载荷；未知字段抛 IncompatibleField，返回规范变更集。"""
    allowed = _ALLOWED_UPDATE_FIELDS[version.number]
    _reject_unknown(version, payload, allowed)
    model = _UPDATE_MODELS[version.number](**payload)
    changes: dict[str, Any] = {}
    if version.number == 1:
        if model.title is not None:
            changes["title"] = model.title
        if model.body is not None:
            changes["body"] = model.body
    else:
        if model.title is not None:
            changes["title"] = model.title
        if model.content is not None:
            changes["content"] = model.content
        if model.tags is not None:
            changes["tags"] = model.tags
    return changes


def render(version: VersionInfo, record: NoteRecord) -> dict[str, Any]:
    """把规范记录投影为协商版本的响应视图（旧版本绝不暴露新字段名）。"""
    if version.number == 1:
        return NoteViewV1(
            id=record.id, title=record.title, body=record.body, revision=record.revision
        ).model_dump()
    return NoteViewV2(
        id=record.id,
        title=record.title,
        content=record.body,
        tags=list(record.tags),
        revision=record.revision,
    ).model_dump()
