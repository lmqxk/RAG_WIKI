from backend.domain import SearchHit
from backend.retrieval import focused_query, query_type, reciprocal_rank_fusion


def make_hit(chunk_id: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id="doc",
        text="正文",
        clause_no=None,
        chapter_path="",
        page_start=1,
        page_end=1,
        printed_page=None,
        document_title="规范",
        standard_no=None,
        version=None,
        score=0,
        source="test",
        content_type="normative",
    )


def test_query_type_detects_comparison_and_clause() -> None:
    assert query_type("对比新旧规范的变化") == "comparison"
    assert query_type("5.2.1 条是什么") == "clause"


def test_focused_query_removes_document_titles_and_comparison_noise() -> None:
    focused = focused_query(
        "对比《规范甲》与《规范乙》对锅炉房防火分隔和位置的要求有哪些变化？",
        "comparison",
    )

    assert "规范甲" not in focused
    assert "对比" not in focused
    assert "锅炉房" in focused
    assert "防火分隔" in focused


def test_rrf_rewards_hits_in_multiple_lists() -> None:
    first = [make_hit("a"), make_hit("b")]
    second = [make_hit("b"), make_hit("c")]

    fused = reciprocal_rank_fusion([first, second])

    assert fused[0].chunk_id == "b"
