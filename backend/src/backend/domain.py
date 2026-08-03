"""领域模型模块：定义解析文档、页面块、文本块和检索结果等核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PageBlock:
    page: int
    text: str
    block_type: str = "text"
    bbox: list[float] = field(default_factory=list)
    level: int | None = None
    printed_page: str | None = None
    images: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class ParsedDocument:
    pages: int
    blocks: list[PageBlock]
    markdown: str
    parser_name: str
    needs_ocr: bool


@dataclass(slots=True)
class Chunk:
    id: str
    document_id: str
    ordinal: int
    chapter_path: str
    clause_no: str | None
    text: str
    page_start: int
    page_end: int
    printed_page: str | None = None
    source_type: str = "text"
    parent_id: str | None = None


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    document_id: str
    text: str
    clause_no: str | None
    chapter_path: str
    page_start: int
    page_end: int
    printed_page: str | None
    document_title: str
    standard_no: str | None
    version: str | None
    score: float
    source: str
    content_type: str = "normative"
