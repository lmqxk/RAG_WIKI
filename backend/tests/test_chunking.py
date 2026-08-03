from backend.chunking import build_chunks
from backend.domain import PageBlock


def test_builds_clause_chunks_with_page_metadata() -> None:
    blocks = [
        PageBlock(page=1, text="5 建筑结构"),
        PageBlock(page=1, text="5.2 工业建筑"),
        PageBlock(page=2, text="5.2.1 下列工业建筑的耐火等级应为一级："),
        PageBlock(page=2, text="1 建筑高度大于50m的高层厂房；", printed_page="22"),
        PageBlock(page=3, text="5.2.2 其他工业建筑的耐火等级不应低于二级。"),
    ]

    chunks = build_chunks("doc-1", blocks)

    assert [chunk.clause_no for chunk in chunks] == ["5.2.1", "5.2.2"]
    assert chunks[0].chapter_path == "5 建筑结构 > 5.2 工业建筑"
    assert chunks[0].page_start == 2
    assert "50m" in chunks[0].text


def test_splits_long_clause_without_losing_clause_reference() -> None:
    text = "1.0.1 " + "本规范适用于建筑设计。" * 200
    chunks = build_chunks("doc-1", [PageBlock(page=1, text=text)])

    assert len(chunks) > 1
    assert chunks[0].clause_no == "1.0.1"
    assert chunks[1].clause_no == "1.0.1#2"


def test_keeps_table_and_image_chunks_searchable() -> None:
    blocks = [
        PageBlock(page=1, text="表格内容：耐火等级 一级 二级", block_type="table"),
        PageBlock(
            page=2,
            text="图片标题：防火分区示意图 图片文件：images/page2.png",
            block_type="image",
        ),
    ]

    chunks = build_chunks("doc-1", blocks)

    assert [chunk.source_type for chunk in chunks] == ["normative_table", "normative_image"]
    assert "耐火等级" in chunks[0].text
    assert "防火分区示意图" in chunks[1].text
