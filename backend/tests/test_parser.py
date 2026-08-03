from pathlib import Path

from backend.config import Settings
from backend.domain import ParsedDocument
from backend.parser import DocumentParser, MinerUParser, _table_images


def test_table_images_extracts_caption_row_and_column_context() -> None:
    html = (
        "<table>"
        "<tr><td colspan=2>构件名称</td><td>截面图和结构厚度或截面最小尺寸(mm)</td></tr>"
        "<tr><td>屋顶承重构件</td><td>屋顶橡条或轻型木桁架</td>"
        '<td>橡檩屋顶截面<img src="images/c0fb.jpg"/>'
        '0.50轻型木桁架屋顶截面<img src="images/f078.jpg"/></td></tr>'
        "</table>"
    )

    images = _table_images(html)

    assert images == [
        {
            "path": "images/c0fb.jpg",
            "caption": "橡檩屋顶截面",
            "row_context": "屋顶承重构件",
            "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
            "cell_text": "橡檩屋顶截面0.50轻型木桁架屋顶截面",
            "order": 1,
        },
        {
            "path": "images/f078.jpg",
            "caption": "轻型木桁架屋顶截面",
            "row_context": "屋顶承重构件",
            "column_context": "截面图和结构厚度或截面最小尺寸(mm)",
            "cell_text": "橡檩屋顶截面0.50轻型木桁架屋顶截面",
            "order": 2,
        },
    ]


def test_mineru_item_text_keeps_table_and_image_metadata(tmp_path) -> None:
    parser = MinerUParser(Settings(data_dir=tmp_path, mineru_model_dir=tmp_path / "models"))

    text = parser._item_text(
        {
            "type": "table",
            "table_caption": ["表 1 防火间距"],
            "table_body": "<table><tr><td>一级</td></tr></table>",
            "table_footnote": ["注：单位为 m"],
            "img_path": "images/table1.png",
        }
    )

    assert "表格标题：表 1 防火间距" in text
    assert "表格内容：<table>" in text
    assert "表格注释：注：单位为 m" in text
    assert "图片文件：images/table1.png" in text


def test_pdf_extract_kit_pipeline_is_default_document_pipeline(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path, mineru_model_dir=tmp_path / "models")
    parser = MinerUParser(settings)

    assert settings.document_pipeline == "pdf-extract-kit"
    assert settings.mineru_backend is None
    assert parser.cli_backend() == "pipeline"
    assert parser.parser_name() == "opendatalab-pdf-extract-kit"
    assert parser.parser_label() == "OpenDataLab PDF-Extract-Kit 1.0"


def test_document_pipeline_alias_switches_to_mineru_vlm(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        mineru_model_dir=tmp_path / "models",
        document_pipeline="mineru",
    )
    parser = MinerUParser(settings)

    assert parser.cli_backend() == "vlm-engine"
    assert parser.parser_name() == "mineru-vlm-engine"
    assert parser.parser_label() == "MinerU VLM"


def test_scan_parser_pdf_extract_kit_legacy_value_still_uses_cli(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        mineru_model_dir=tmp_path / "models",
        document_pipeline="unknown",
        scan_parser="pdf-extract-kit",
    )
    parser = DocumentParser(settings)
    calls = {"pdf_extract_kit": 0}

    class DummyParser:
        def available(self) -> bool:
            return True

        def parse(self, path: Path, output_dir: Path, progress) -> ParsedDocument:
            calls["pdf_extract_kit"] += 1
            return ParsedDocument(
                pages=1,
                blocks=[],
                markdown="",
                parser_name="opendatalab-pdf-extract-kit",
                needs_ocr=True,
            )

    monkeypatch.setattr("backend.parser.inspect_pdf", lambda path, max_pages: (1, False))
    parser.mineru = DummyParser()

    parsed = parser.parse(Path("fake.pdf"), tmp_path / "out", lambda value, message: None)

    assert parsed.parser_name == "opendatalab-pdf-extract-kit"
    assert calls["pdf_extract_kit"] == 1


def test_document_parser_prefers_pdf_extract_kit_for_text_pdf(monkeypatch, tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        mineru_model_dir=tmp_path / "models",
        scan_parser="mineru",
    )
    parser = DocumentParser(settings)
    calls = {"pdf_extract_kit": 0}

    class DummyMinerU:
        def available(self) -> bool:
            return True

        def parse(self, path: Path, output_dir: Path, progress) -> ParsedDocument:
            calls["pdf_extract_kit"] += 1
            return ParsedDocument(
                pages=1,
                blocks=[],
                markdown="",
                parser_name="opendatalab-pdf-extract-kit",
                needs_ocr=True,
            )

    monkeypatch.setattr("backend.parser.inspect_pdf", lambda path, max_pages: (1, False))
    parser.mineru = DummyMinerU()

    parsed = parser.parse(Path("fake.pdf"), tmp_path / "out", lambda value, message: None)

    assert parsed.parser_name == "opendatalab-pdf-extract-kit"
    assert calls["pdf_extract_kit"] == 1
