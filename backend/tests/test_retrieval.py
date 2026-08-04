from backend.domain import SearchHit
from backend.retrieval import (
    documents_for_comparison,
    focused_query,
    plan_query,
    query_type,
    reciprocal_rank_fusion,
)


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


def test_plan_query_targets_mentioned_ready_documents_for_comparison() -> None:
    documents = [
        {
            "id": "new",
            "title": "建筑防火通用规范",
            "filename": "GB55037-2022.pdf",
            "standard_no": "GB55037-2022",
            "version": "2022",
            "status": "READY",
        },
        {
            "id": "old",
            "title": "建筑设计防火规范",
            "filename": "GB50016-2014.pdf",
            "standard_no": "GB50016-2014",
            "version": "2018",
            "status": "READY",
        },
        {
            "id": "failed",
            "title": "未完成规范",
            "filename": "failed.pdf",
            "standard_no": "GB99999-2020",
            "version": "2020",
            "status": "FAILED",
        },
    ]

    plan = plan_query("对比 GB55037-2022 和 GB50016-2014 的防火间距差异", None, documents)

    assert plan.kind == "comparison"
    assert plan.search_question == "GB55037-2022 GB50016-2014 的防火间距"
    assert plan.target_document_ids == ["new", "old"]


def test_documents_for_comparison_falls_back_to_ready_documents_for_new_old_query() -> None:
    documents = [
        {"id": "draft", "title": "草稿", "version": "2020", "status": "FAILED"},
        {"id": "old", "title": "规范", "version": "2018", "status": "READY"},
        {"id": "new", "title": "规范", "version": "2022", "status": "READY"},
    ]

    assert documents_for_comparison("新旧规范防火间距有什么变化", documents) == [
        "new",
        "old",
    ]


def test_rrf_rewards_hits_in_multiple_lists() -> None:
    first = [make_hit("a"), make_hit("b")]
    second = [make_hit("b"), make_hit("c")]

    fused = reciprocal_rank_fusion([first, second])

    assert fused[0].chunk_id == "b"
