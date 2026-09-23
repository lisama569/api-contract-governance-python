"""资源存储与基于 revision 的乐观并发控制（进程内、单事件循环）。

每次成功写入都会令 ``revision`` 自增；更新必须携带期望的 revision，
revision 不匹配说明客户端基于陈旧状态做决策，明确拒绝（STALE_WRITE）。
``side_effect_count`` 统计真实落库次数，供测试断言副作用只发生一次。

可注入异步 ``before_create`` 钩子（测试用它在首个写入前让出事件循环，
制造真实的并发重叠窗口）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.errors import AppError, ErrorCode

logger = logging.getLogger("app")

BeforeCreateHook = Callable[[str], Awaitable[None]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Document:
    id: str
    title: str
    body: str
    owner: str
    tags: list[str] = field(default_factory=list)
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""


class DocumentStore:
    """进程内文档存储（所有写操作在关键区内不 await，天然对单事件循环安全）。"""

    def __init__(self, before_create: BeforeCreateHook | None = None) -> None:
        self._documents: dict[str, Document] = {}
        self._side_effect_count = 0
        self._before_create = before_create

    async def create(self, document_id: str, fields: dict) -> Document:
        if self._before_create is not None:
            await self._before_create(document_id)
        if document_id in self._documents:
            raise AppError(
                ErrorCode.STATE_CONFLICT,
                f"文档 {document_id} 已存在",
                details={"document_id": document_id},
            )
        now = _now_iso()
        document = Document(
            id=document_id,
            title=fields["title"],
            body=fields["body"],
            owner=fields["owner"],
            tags=list(fields.get("tags", [])),
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._documents[document_id] = document
        self._side_effect_count += 1
        logger.info(
            "event=document_created id=%s revision=0 side_effect_count=%s",
            document_id,
            self._side_effect_count,
        )
        return document

    async def get(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)

    async def replace(
        self, document_id: str, fields: dict, expected_revision: int
    ) -> Document:
        document = self._documents.get(document_id)
        if document is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"文档 {document_id} 不存在",
                details={"document_id": document_id},
            )
        if document.revision != expected_revision:
            logger.info(
                "event=stale_write_rejected id=%s expected_revision=%s actual_revision=%s",
                document_id,
                expected_revision,
                document.revision,
            )
            raise AppError(
                ErrorCode.STALE_WRITE,
                "资源已被其他请求修改，请基于最新版本重试",
                details={
                    "document_id": document_id,
                    "expected_revision": expected_revision,
                    "actual_revision": document.revision,
                },
            )
        document.title = fields["title"]
        document.body = fields["body"]
        document.owner = fields["owner"]
        document.tags = list(fields.get("tags", []))
        document.revision += 1
        document.updated_at = _now_iso()
        self._side_effect_count += 1
        logger.info(
            "event=document_replaced id=%s revision=%s side_effect_count=%s",
            document_id,
            document.revision,
            self._side_effect_count,
        )
        return document

    @property
    def side_effect_count(self) -> int:
        return self._side_effect_count
