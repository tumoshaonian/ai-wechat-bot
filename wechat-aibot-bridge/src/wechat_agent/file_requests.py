"""Deterministic local-file delivery for explicit natural-language requests."""

from __future__ import annotations

import re
from pathlib import Path

from .domain import AgentReply


_SEND_MARKERS = ("给我发送", "发送给我", "发给我", "传给我", "给我传")
_DESKTOP_MARKERS = ("电脑桌面", "桌面上", "桌面")
_DOCUMENT_EXTENSIONS = (".docx", ".doc", ".odt", ".rtf", ".pdf", ".txt", ".md")
_TRIM_PREFIXES = (
    "麻烦你帮我把",
    "麻烦你把",
    "请你帮我把",
    "请你把",
    "你帮我把",
    "帮我把",
    "请把",
    "麻烦你",
    "请你",
    "麻烦",
    "帮我",
    "请",
    "把",
)
_GENERIC_WORDS = (
    "电脑桌面上的",
    "电脑桌面的",
    "电脑桌面上",
    "电脑桌面",
    "桌面上的",
    "桌面的",
    "桌面上",
    "桌面",
    "给我发送",
    "发送给我",
    "发给我",
    "传给我",
    "给我传",
    "这个",
    "那个",
)
_TRAILING_KINDS = ("文档文件", "文档", "文件")


class DesktopFileRequestResolver:
    """Resolve an unambiguous request for a file located on the user's desktop."""

    def __init__(self, desktop_directory: Path | None = None) -> None:
        self._desktop = (desktop_directory or (Path.home() / "Desktop")).resolve()

    def resolve(self, content: str) -> str | AgentReply | None:
        """Return None when this is not an explicit desktop-file send request."""

        if not _is_desktop_send_request(content):
            return None
        query = _extract_query(content)
        if not query:
            return "请告诉我要发送的桌面文件名称。"
        if not self._desktop.is_dir():
            return f"找不到电脑桌面目录：{self._desktop}"

        matches = _rank_matches(query, content, self._desktop)
        if not matches:
            return f"没有在电脑桌面找到名称包含“{query}”的文件。"
        best_score = matches[0][0]
        best = [path for score, path in matches if score == best_score]
        if len(best) > 1:
            names = "、".join(path.name for path in best[:5])
            return f"桌面上找到多个同样匹配的文件：{names}。请说出完整文件名或扩展名。"
        path = best[0]
        return AgentReply(f"已在电脑桌面找到文件，准备发送：{path.name}", (path,))


def _is_desktop_send_request(content: str) -> bool:
    compact = re.sub(r"\s+", "", content)
    return any(marker in compact for marker in _SEND_MARKERS) and any(
        marker in compact for marker in _DESKTOP_MARKERS
    )


def _extract_query(content: str) -> str:
    value = content.strip().strip("。！!？?，,")
    for prefix in _TRIM_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :].lstrip()
            break
    for word in _GENERIC_WORDS:
        value = value.replace(word, "")
    value = value.strip(" 的。！!？?，,：:\"'")
    for kind in _TRAILING_KINDS:
        if value.endswith(kind):
            value = value[: -len(kind)].rstrip()
            break
    return value.strip(" 的。！!？?，,：:\"'")


def _rank_matches(query: str, original: str, desktop: Path) -> list[tuple[int, Path]]:
    normalized_query = _normalized_name(query)
    requested_suffix = Path(query).suffix.casefold()
    requested_document = "文档" in original and not requested_suffix
    ranked: list[tuple[int, Path]] = []
    for path in desktop.iterdir():
        if not path.is_file():
            continue
        normalized_stem = _normalized_name(path.stem)
        normalized_name = _normalized_name(path.name)
        if not normalized_query:
            continue
        if requested_suffix and normalized_name == normalized_query:
            name_score = 500
        elif normalized_stem == normalized_query:
            name_score = 450
        elif normalized_query in normalized_stem:
            name_score = 350 - min(100, len(normalized_stem) - len(normalized_query))
        elif normalized_stem in normalized_query:
            name_score = 250 - min(100, len(normalized_query) - len(normalized_stem))
        else:
            continue

        suffix = path.suffix.casefold()
        type_score = 0
        if requested_suffix:
            type_score = 100 if suffix == requested_suffix else -100
        elif requested_document and suffix in _DOCUMENT_EXTENSIONS:
            type_score = 50 - _DOCUMENT_EXTENSIONS.index(suffix)
        ranked.append((name_score + type_score, path.resolve()))
    return sorted(ranked, key=lambda item: (-item[0], item[1].name.casefold()))


def _normalized_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())
