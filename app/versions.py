"""接口版本注册表与版本协商。

协商入口（两种，结论一致）：
  1. URL 前缀：/api/{version_token}/notes
  2. 请求头：Api-Version: {version_token}

版本标识形态：
  - 主版本别名：v0 / v1 / v2
  - 发布日期别名：YYYY-MM-DD（解析为该日期当天生效的版本）

失败必须可区分：
  - 无法识别（拼写错误、未来版本号、早于首个发布日） -> VERSION_NOT_FOUND
  - 可识别但已停用（v0）                        -> VERSION_UNSUPPORTED
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class VersionStatus(str, Enum):
    CURRENT = "current"
    SUPPORTED = "supported"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


@dataclass(frozen=True)
class VersionInfo:
    name: str  # v1 / v2 ...
    number: int
    status: VersionStatus
    released_at: str  # ISO 日期


# 已注册版本（含一个已停用样例 v0，用于区分“未知”与“不支持”）
_REGISTRY: tuple[VersionInfo, ...] = (
    VersionInfo("v0", 0, VersionStatus.RETIRED, "2023-01-01"),
    VersionInfo("v1", 1, VersionStatus.DEPRECATED, "2024-01-01"),
    VersionInfo("v2", 2, VersionStatus.CURRENT, "2025-06-01"),
)
_BY_NAME = {v.name: v for v in _REGISTRY}
_BY_NUMBER = {v.number: v for v in _REGISTRY}

_TOKEN_RE = re.compile(r"^(v\d+|\d{4}-\d{2}-\d{2})$", re.IGNORECASE)


class VersionNegotiationResult(NamedTuple):
    version: VersionInfo | None
    # None=成功; "not_found"=无法识别; "unsupported"=已识别但停用
    failure: str | None


def _resolve_token(token: str) -> VersionInfo | None:
    token = token.strip().lower()
    if not _TOKEN_RE.match(token):
        return None
    if token.startswith("v"):
        return _BY_NAME.get(token)
    # 日期别名：取 released_at <= token 的最新版本
    candidates = [v for v in _REGISTRY if v.released_at <= token]
    return max(candidates, key=lambda v: v.number, default=None)


def negotiate(token: str | None) -> VersionNegotiationResult:
    """把路径片段或请求头值解析为已注册版本。"""
    if token is None or not token.strip():
        return VersionNegotiationResult(None, "not_found")
    info = _resolve_token(token)
    if info is None:
        return VersionNegotiationResult(None, "not_found")
    if info.status is VersionStatus.RETIRED:
        return VersionNegotiationResult(info, "unsupported")
    return VersionNegotiationResult(info, None)


def all_versions() -> tuple[VersionInfo, ...]:
    return _REGISTRY
