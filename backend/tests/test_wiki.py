from pathlib import Path

from backend.config import Settings
from backend.domain import Chunk, PageBlock, ParsedDocument, SearchHit
from backend.wiki import WikiManager


class FakeGenerator:
    def generate_json(self, system_prompt, user_payload, *, max_tokens=1200):
        assert "唯一原文事实来源" in system_prompt
        if "documents" in user_payload:
            return {
                "summary": "建筑防火知识库概览。",
                "themes": ["工业建筑", "防火分区"],
                "gaps": ["待审核跨文档关系"],
            }
        chunk_id = user_payload["source_chunks"][0]["chunk_id"]
        return {
            "summary": "工业建筑防火要求的结构化资料。",
            "topics": ["工业建筑", "防火分区"],
            "concepts": [
                {
                    "name": "防火分区",
                    "aliases": ["建筑防火分区"],
                    "dimensions": ["最大允许建筑面积"],
                    "source_chunk_ids": [chunk_id, "not-a-real-chunk"],
                    "related_concepts": ["防火间距"],
                },
                {
                    "name": "防火间距",
                    "aliases": ["建筑间距"],
                    "dimensions": ["最小距离"],
                    "source_chunk_ids": [user_payload["source_chunks"][1]["chunk_id"]],
                    "related_concepts": ["防火分区"],
                },
            ],
        }


def test_wiki_sync_generates_traceable_document_and_concept_pages(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, wiki_llm_enabled=True)
    manager = WikiManager(settings, FakeGenerator())
    chunks = [
        Chunk(
            id="chunk-1",
            document_id="doc-1",
            ordinal=1,
            chapter_path="第 3 章 工业建筑",
            clause_no="3.1.1",
            text="甲类厂房的防火分区最大允许建筑面积应符合规定。",
            page_start=12,
            page_end=12,
        ),
        Chunk(
            id="chunk-2",
            document_id="doc-1",
            ordinal=2,
            chapter_path="第 3 章 工业建筑",
            clause_no="3.2.1",
            text="厂房之间的防火间距应符合规定。",
            page_start=13,
            page_end=13,
        ),
    ]
    parsed = ParsedDocument(
        pages=20,
        blocks=[PageBlock(page=12, text=chunks[0].text)],
        markdown="# 工业建筑",
        parser_name="opendatalab-pdf-extract-kit",
        needs_ocr=False,
    )
    document = {
        "id": "doc-1",
        "title": "建筑防火通用规范",
        "filename": "GB55037-2022.pdf",
        "standard_no": "GB55037-2022",
        "version": "2022",
        "sha256": "abc",
    }

    manager.sync_document(document, parsed, chunks)

    metadata = (tmp_path / "wiki" / "metadata" / "doc-1.json").read_text(encoding="utf-8")
    document_page = (tmp_path / "wiki" / "documents" / "doc-1.md").read_text(encoding="utf-8")
    concept_page = (tmp_path / "wiki" / "concepts" / "防火分区.md").read_text(encoding="utf-8")
    overview = (tmp_path / "wiki" / "overview.md").read_text(encoding="utf-8")
    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    related = (tmp_path / "wiki" / "graph" / "related.json").read_text(encoding="utf-8")
    assert '"chunk-1"' in metadata
    assert "not-a-real-chunk" not in metadata
    assert "[[防火分区]]" in document_page
    assert "[[documents/doc-1|建筑防火通用规范]]：`chunk-1`" in concept_page
    assert "[[防火间距]]" in concept_page
    assert "建筑防火知识库概览" in overview
    assert " ingest | 建筑防火通用规范" in log
    assert "direct_wikilink" in related
    assert manager.catalog_context([document])[0]["concepts"] == ["防火分区", "防火间距"]

    manager.sync_document(document, parsed, chunks, event="reindex")
    updated_page = (tmp_path / "wiki" / "documents" / "doc-1.md").read_text(encoding="utf-8")
    assert updated_page.count("## 计算相关") == 1
    assert "[[concepts/防火分区|防火分区]]" in updated_page
    manager.log_query(
        "甲类厂房防火分区要求",
        "single_query",
        [
            SearchHit(
                chunk_id="chunk-1",
                document_id="doc-1",
                text=chunks[0].text,
                clause_no="3.1.1",
                chapter_path="第 3 章 工业建筑",
                page_start=12,
                page_end=12,
                printed_page=None,
                document_title="建筑防火通用规范",
                standard_no="GB55037-2022",
                version="2022",
                score=1.0,
                source="hybrid",
            )
        ],
    )
    updated_log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert updated_log.count("## [") == 3
    assert " reindex | 建筑防火通用规范" in updated_log
    assert " query | 甲类厂房防火分区要求" in updated_log


def test_wiki_sync_falls_back_without_llm_result(tmp_path: Path) -> None:
    class EmptyGenerator:
        def generate_json(self, system_prompt, user_payload, *, max_tokens=1200):
            return None

    settings = Settings(data_dir=tmp_path, wiki_llm_enabled=True)
    manager = WikiManager(settings, EmptyGenerator())
    chunks = [
        Chunk(
            id="chunk-1",
            document_id="doc-1",
            ordinal=1,
            chapter_path="第 1 章 总则",
            clause_no=None,
            text="本规范适用于建筑防火设计。",
            page_start=1,
            page_end=1,
        )
    ]
    parsed = ParsedDocument(1, [], "# 总则", "parser", False)
    manager.sync_document({"id": "doc-1", "title": "测试规范"}, parsed, chunks)

    metadata = (tmp_path / "wiki" / "metadata" / "doc-1.json").read_text(encoding="utf-8")
    assert '"generation": "fallback"' in metadata
