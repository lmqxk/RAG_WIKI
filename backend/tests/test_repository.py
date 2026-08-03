from pathlib import Path

from backend.db import Database
from backend.domain import Chunk
from backend.repository import Repository, search_terms


def test_chinese_search_terms_include_bigrams() -> None:
    terms = search_terms("工业建筑耐火等级 GB55037")
    assert "工业" in terms
    assert "耐火" in terms
    assert "gb55037" in terms


def test_bm25_search_finds_clause(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    repository.create_document(
        document_id="doc-1",
        filename="规范.pdf",
        stored_path=tmp_path / "规范.pdf",
        sha256="abc",
        title="建筑防火通用规范",
        standard_no="GB 55037-2022",
        version="2022",
        file_size=10,
    )
    repository.update_document("doc-1", status="READY")
    repository.replace_chunks(
        "doc-1",
        [
            Chunk(
                id="chunk-1",
                document_id="doc-1",
                ordinal=0,
                chapter_path="5 建筑结构 > 5.2 工业建筑",
                clause_no="5.2.1",
                text="下列工业建筑的耐火等级应为一级。",
                page_start=22,
                page_end=22,
            )
        ],
    )

    hits = repository.bm25_search(
        "工业建筑耐火等级",
        document_ids=None,
        limit=5,
    )

    assert hits
    assert hits[0].clause_no == "5.2.1"


def test_get_chunks_only_returns_ready_documents(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    repository.create_document(
        document_id="doc-1",
        filename="瑙勮寖.pdf",
        stored_path=tmp_path / "瑙勮寖.pdf",
        sha256="abc",
        title="寤虹瓚闃茬伀閫氱敤瑙勮寖",
        standard_no="GB 55037-2022",
        version="2022",
        file_size=10,
    )
    repository.replace_chunks(
        "doc-1",
        [
            Chunk(
                id="chunk-1",
                document_id="doc-1",
                ordinal=0,
                chapter_path="5 寤虹瓚缁撴瀯",
                clause_no="5.2.1",
                text="旧解析内容",
                page_start=22,
                page_end=22,
            )
        ],
    )

    assert repository.get_chunks(["chunk-1"]) == []

    repository.update_document("doc-1", status="READY")

    assert [hit.chunk_id for hit in repository.get_chunks(["chunk-1"])] == ["chunk-1"]
