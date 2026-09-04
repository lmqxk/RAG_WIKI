"""验证 HTML 表格按行切片，避免整表干扰检索。"""

from backend.chunking import build_chunks
from backend.domain import PageBlock


def test_splits_html_table_into_row_chunks_with_rowspan_context() -> None:
    header_row = (
        "<tr><td>序号</td><td colspan=2>构件名称</td><td>构件厚度(mm)</td>"
        "<td>耐火极限(h)</td><td>燃烧性能</td></tr>"
    )
    table = (
        "表格内容：<table>"
        f"{header_row}"
        "<tr><td rowspan=2>8</td><td rowspan=2>石膏空心条板隔墙</td>"
        "<td>1. 石膏珍珠岩空心条板</td><td>60</td><td>1.50</td><td>不燃性</td></tr>"
        "<tr><td>2. 石膏珍珠岩塑料网空心条板</td><td>60</td><td>1.30</td><td>不燃性</td></tr>"
        "</table>"
    )

    chunks = build_chunks("doc-1", [PageBlock(page=2, text=table, block_type="table")])

    assert len(chunks) == 2
    assert all(chunk.source_type == "normative_table" for chunk in chunks)
    assert "石膏空心条板隔墙" in chunks[0].text
    assert "1.50" in chunks[0].text
    assert "不燃性" in chunks[0].text
    assert "石膏空心条板隔墙" in chunks[1].text
    assert "1.30" in chunks[1].text
