"""内存资源库：revision 乐观并发控制 + 副作用计数（测试断言用）。"""
from __future__ import annotations

import threading

from app.contracts import NoteRecord


class ConcurrentUpdateError(Exception):
    """If-Match revision 与当前 revision 不一致 -> 陈旧写入。"""

    def __init__(self, expected: int, actual: int, resource_id: str) -> None:
        self.expected = expected
        self.actual = actual
        self.resource_id = resource_id
        super().__init__(f"stale revision: expected {expected}, actual {actual}")


class ResourceNotFoundError(Exception):
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id
        super().__init__(f"note not found: {resource_id}")


class NoteRepository:
    def __init__(self) -> None:
        self._notes: dict[str, NoteRecord] = {}
        self._lock = threading.RLock()
        self._counter = 0
        # 真实副作用次数（写库次数），供测试断言
        self.side_effect_count = 0

    def _next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"note-{self._counter}"

    def create(self, title: str, body: str, tags: list[str]) -> NoteRecord:
        with self._lock:
            note = NoteRecord(id=self._next_id(), title=title, body=body, tags=list(tags), revision=1)
            self._notes[note.id] = note
            self.side_effect_count += 1
            return note.model_copy()

    def get(self, note_id: str) -> NoteRecord:
        with self._lock:
            note = self._notes.get(note_id)
            if note is None:
                raise ResourceNotFoundError(note_id)
            return note.model_copy()

    def update(self, note_id: str, expected_revision: int, changes: dict) -> NoteRecord:
        with self._lock:
            note = self._notes.get(note_id)
            if note is None:
                raise ResourceNotFoundError(note_id)
            if note.revision != expected_revision:
                raise ConcurrentUpdateError(expected_revision, note.revision, note_id)
            # 规范字段名 -> 存储字段：content 是 v2 对 body 的改名
            if "content" in changes:
                changes = {**changes, "body": changes.pop("content")}
            updated = note.model_copy(update={**changes, "revision": note.revision + 1})
            self._notes[note_id] = updated
            self.side_effect_count += 1
            return updated.model_copy()

    def delete(self, note_id: str, expected_revision: int) -> None:
        with self._lock:
            note = self._notes.get(note_id)
            if note is None:
                raise ResourceNotFoundError(note_id)
            if note.revision != expected_revision:
                raise ConcurrentUpdateError(expected_revision, note.revision, note_id)
            del self._notes[note_id]
            self.side_effect_count += 1

    def reset(self) -> None:
        """清空数据与计数器（测试隔离用）。"""
        with self._lock:
            self._notes.clear()
            self._counter = 0
            self.side_effect_count = 0
