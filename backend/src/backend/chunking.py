"""文档切分模块：按章节、条款和页码生成可检索文本块，并保留父子结构。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
from uuid import uuid4

from .domain import Chunk, PageBlock

CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){2,5})\s*(.+)$")
SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+([\u3400-\u9fff].{0,80})$")
TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._current_row: list[tuple[str, int, int]] | None = None
        self._current_cell: list[str] | None = None
        self._rowspan = 1
        self._colspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            values = dict(attrs)
            self._current_cell = []
            self._rowspan = _positive_int(values.get("rowspan"), 1)
            self._colspan = _positive_int(values.get("colspan"), 1)
        elif tag == "img" and self._current_cell is not None:
            src = dict(attrs).get("src")
            if src:
                self._current_cell.append(f" 图片文件：{src} ")

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag in {"td", "th"}
            and self._current_row is not None
            and self._current_cell is not None
        ):
            text = _clean(unescape("".join(self._current_cell)))
            self._current_row.append((text, self._rowspan, self._colspan))
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
CHINESE_HEADING_RE = re.compile(r"^\s*第[一二三四五六七八九十百]+[编篇章节]\s*.*$")


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", text)
    return text


def _split_text(text: str, limit: int = 1400, overlap: int = 180) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = [part for part in re.split(r"(?<=[。！？；])", text) if part]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > limit:
            parts.append(current.strip())
            current = current[-overlap:] + sentence
        else:
            current += sentence
    if current.strip():
        parts.append(current.strip())
    return parts


def _expanded_table_rows(html: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html)
    active: dict[int, tuple[int, str]] = {}
    rows: list[list[str]] = []
    for raw_row in parser.rows:
        row: list[str] = []
        column = 0

        def consume_active(target_row: list[str]) -> None:
            nonlocal column
            while column in active:
                remaining, value = active.pop(column)
                target_row.append(value)
                if remaining > 1:
                    active[column] = (remaining - 1, value)
                column += 1

        consume_active(row)
        for value, rowspan, colspan in raw_row:
            consume_active(row)
            for offset in range(colspan):
                row.append(value)
                if rowspan > 1:
                    active[column + offset] = (rowspan - 1, value)
            column += colspan
        consume_active(row)
        if any(cell for cell in row):
            rows.append(row)
    return rows


def _table_row_texts(text: str) -> list[str]:
    table_match = TABLE_RE.search(text)
    if not table_match:
        return []
    rows = _expanded_table_rows(table_match.group(0))
    if len(rows) <= 1:
        return []
    header = rows[0]
    result: list[str] = []
    for row in rows[1:]:
        parts: list[str] = []
        for index, value in enumerate(row):
            if not value:
                continue
            heading = header[index] if index < len(header) else ""
            if heading and heading != value:
                parts.append(f"{heading}：{value}")
            else:
                parts.append(value)
        row_text = _clean("；".join(parts))
        if len(row_text) >= 8:
            result.append(f"表格行：{row_text}")
    return result


def build_chunks(document_id: str, blocks: Iterable[PageBlock]) -> list[Chunk]:
    chunks: list[Chunk] = []
    chapter_path: list[str] = []
    current_clause: str | None = None
    current_text: list[str] = []
    current_start = 1
    current_end = 1
    current_printed_page: str | None = None
    document_zone = "normative"
    current_source = document_zone

    def flush() -> None:
        nonlocal current_clause, current_text, current_start, current_end
        if not current_text:
            return
        text = _clean(" ".join(current_text))
        if len(text) < 8:
            current_text = []
            return
        for part_index, part in enumerate(_split_text(text)):
            clause_no = current_clause
            if clause_no and part_index:
                clause_no = f"{clause_no}#{part_index + 1}"
            chunks.append(
                Chunk(
                    id=str(uuid4()),
                    document_id=document_id,
                    ordinal=len(chunks),
                    chapter_path=" > ".join(chapter_path),
                    clause_no=clause_no,
                    text=part,
                    page_start=current_start,
                    page_end=current_end,
                    printed_page=current_printed_page,
                    source_type=current_source,
                )
            )
        current_text = []
        current_clause = None

    for block in sorted(blocks, key=lambda item: (item.page, item.bbox[1] if item.bbox else 0)):
        text = _clean(block.text)
        if not text:
            continue
        if "条文说明" in text and len(text) <= 20:
            flush()
            document_zone = "commentary"
            current_source = document_zone
            chapter_path = ["条文说明"]
            continue
        if block.block_type in {"table", "table_body"}:
            flush()
            current_start = current_end = block.page
            current_printed_page = block.printed_page
            current_source = f"{document_zone}_table"
            for table_text in _table_row_texts(text) or [text]:
                current_text = [table_text]
                flush()
            current_source = document_zone
            continue
        if block.block_type in {"image", "img", "figure"}:
            flush()
            current_start = current_end = block.page
            current_printed_page = block.printed_page
            current_source = f"{document_zone}_image"
            current_text = [text]
            flush()
            current_source = document_zone
            continue

        section_match = SECTION_RE.match(text)
        looks_like_numbered_heading = (
            section_match
            and section_match.group(1).count(".") <= 1
            and len(text) <= 40
            and not text.endswith(("。", "；", ";", "：", ":"))
        )
        if CHINESE_HEADING_RE.match(text) or looks_like_numbered_heading:
            flush()
            heading = text[:100]
            if section_match and "." in section_match.group(1):
                chapter_path = chapter_path[:1] + [heading]
            else:
                chapter_path = [heading]
            continue

        clause_match = CLAUSE_RE.match(text)
        if clause_match:
            flush()
            current_clause = clause_match.group(1)
            current_text = [text]
            current_start = current_end = block.page
            current_printed_page = block.printed_page
            current_source = document_zone
            continue

        if not current_text:
            current_start = block.page
            current_printed_page = block.printed_page
        elif block.printed_page and current_printed_page is None:
            current_printed_page = block.printed_page
        current_end = block.page
        current_text.append(text)
    flush()

    clause_ids = {
        chunk.clause_no: chunk.id
        for chunk in chunks
        if chunk.clause_no and "#" not in chunk.clause_no
    }
    resolved: list[Chunk] = []
    for chunk in chunks:
        base_clause = (chunk.clause_no or "").split("#", 1)[0]
        parent_id = None
        if "." in base_clause:
            parent_id = clause_ids.get(base_clause.rsplit(".", 1)[0])
        resolved.append(replace(chunk, parent_id=parent_id))
    return resolved
