"""表格页 PDF 多方法解析测试。

输出目录默认：test/table1_methods/

方法：
- PyMuPDF text：普通文本层。
- PyMuPDF blocks：版面块。
- PyMuPDF dict：span/line/block 细粒度结构。
- PyMuPDF find_tables：PyMuPDF 表格检测。
- MinerU：强制调用 MinerU Pipeline，不依赖项目是否判定为扫描件。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz

from backend.chunking import build_chunks
from backend.config import Settings
from backend.parser import MinerUParser


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="replace")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render(pdf_path: Path, out_dir: Path) -> None:
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(out_dir / "00-page-1.png")


def pymupdf_text(pdf_path: Path, out_dir: Path) -> dict[str, object]:
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        text = page.get_text("text")
        html = page.get_text("html")
        xhtml = page.get_text("xhtml")
        write_text(out_dir / "01-pymupdf-text.txt", text)
        write_text(out_dir / "02-pymupdf-html.html", html)
        write_text(out_dir / "03-pymupdf-xhtml.xhtml", xhtml)
        return {
            "method": "pymupdf_text",
            "page_count": doc.page_count,
            "page1_text_length": len(text.strip()),
            "page1_text_preview": text.strip()[:500],
        }


def pymupdf_blocks(pdf_path: Path, out_dir: Path) -> dict[str, object]:
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        blocks = [
            {
                "bbox": [float(v) for v in block[:4]],
                "text": " ".join(str(block[4]).split()),
                "block_type": int(block[6]),
            }
            for block in page.get_text("blocks", sort=True)
        ]
        write_json(out_dir / "04-pymupdf-blocks.json", blocks)
        return {
            "method": "pymupdf_blocks",
            "block_count": len(blocks),
            "non_empty_blocks": sum(1 for item in blocks if item["text"]),
        }


def pymupdf_dict(pdf_path: Path, out_dir: Path) -> dict[str, object]:
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        raw = page.get_text("dict", sort=True)
        simplified = []
        for block in raw.get("blocks", []):
            block_item = {
                "type": block.get("type"),
                "bbox": block.get("bbox"),
                "lines": [],
            }
            for line in block.get("lines", []):
                spans = [
                    {
                        "text": span.get("text", ""),
                        "bbox": span.get("bbox"),
                        "font": span.get("font"),
                        "size": span.get("size"),
                    }
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                ]
                if spans:
                    block_item["lines"].append(spans)
            if block_item["lines"]:
                simplified.append(block_item)
        write_json(out_dir / "05-pymupdf-dict.json", simplified)
        return {
            "method": "pymupdf_dict",
            "block_count": len(simplified),
            "line_count": sum(len(item["lines"]) for item in simplified),
        }


def pymupdf_tables(pdf_path: Path, out_dir: Path) -> dict[str, object]:
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        if not hasattr(page, "find_tables"):
            result = {"available": False, "reason": "page.find_tables not available"}
            write_json(out_dir / "06-pymupdf-tables.json", result)
            return {"method": "pymupdf_find_tables", **result}

        tables = page.find_tables()
        payload = []
        for index, table in enumerate(tables.tables, 1):
            extracted = table.extract()
            payload.append(
                {
                    "index": index,
                    "bbox": [float(v) for v in table.bbox],
                    "row_count": len(extracted),
                    "column_count": max((len(row) for row in extracted), default=0),
                    "rows": extracted,
                }
            )
            markdown_lines = []
            if extracted:
                width = max(len(row) for row in extracted)
                normalized = [row + [""] * (width - len(row)) for row in extracted]
                markdown_lines.append("| " + " | ".join(normalized[0]) + " |")
                markdown_lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
                for row in normalized[1:]:
                    markdown_lines.append("| " + " | ".join(cell or "" for cell in row) + " |")
            write_text(out_dir / f"06-pymupdf-table-{index}.md", "\n".join(markdown_lines))
        write_json(out_dir / "06-pymupdf-tables.json", payload)
        return {
            "method": "pymupdf_find_tables",
            "table_count": len(payload),
            "tables": [
                {
                    "index": item["index"],
                    "row_count": item["row_count"],
                    "column_count": item["column_count"],
                }
                for item in payload
            ],
        }


def mineru_pipeline(pdf_path: Path, out_dir: Path) -> dict[str, object]:
    settings = Settings(data_dir=out_dir / "runtime", scan_parser="mineru", max_pdf_pages=20)
    parser = MinerUParser(settings)
    command = parser.command()
    if not command:
        result = {"method": "mineru", "available": False}
        write_json(out_dir / "07-mineru-summary.json", result)
        return result

    def progress(value: float, message: str) -> None:
        print(f"[mineru] {value:5.1f}% {message}")

    parsed = parser.parse(pdf_path, out_dir / "07-mineru-output", progress)
    write_text(out_dir / "07-mineru.md", parsed.markdown)
    normalized = {
        "pages": parsed.pages,
        "parser_name": parsed.parser_name,
        "needs_ocr": parsed.needs_ocr,
        "blocks": [
            {
                "page": block.page,
                "text": block.text,
                "type": block.block_type,
                "bbox": block.bbox,
                "level": block.level,
                "printed_page": block.printed_page,
            }
            for block in parsed.blocks
        ],
    }
    write_json(out_dir / "07-mineru-normalized.json", normalized)
    chunks = build_chunks("table1", parsed.blocks)
    write_json(
        out_dir / "07-mineru-chunks.json",
        [
            {
                "ordinal": chunk.ordinal,
                "chapter_path": chunk.chapter_path,
                "clause_no": chunk.clause_no,
                "text": chunk.text,
                "source_type": chunk.source_type,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
            }
            for chunk in chunks
        ],
    )
    block_types: dict[str, int] = {}
    for block in parsed.blocks:
        block_types[block.block_type] = block_types.get(block.block_type, 0) + 1
    result = {
        "method": "mineru",
        "available": True,
        "command": command,
        "pages": parsed.pages,
        "block_count": len(parsed.blocks),
        "block_types": block_types,
        "markdown_length": len(parsed.markdown),
        "chunk_count": len(chunks),
    }
    write_json(out_dir / "07-mineru-summary.json", result)
    return result


def main() -> None:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--pdf", default="test/table1.pdf")
    arg_parser.add_argument("--out", default="test/table1_methods")
    arg_parser.add_argument("--skip-mineru", action="store_true")
    args = arg_parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    render(pdf_path, out_dir)
    summaries.append(pymupdf_text(pdf_path, out_dir))
    summaries.append(pymupdf_blocks(pdf_path, out_dir))
    summaries.append(pymupdf_dict(pdf_path, out_dir))
    summaries.append(pymupdf_tables(pdf_path, out_dir))
    if not args.skip_mineru:
        summaries.append(mineru_pipeline(pdf_path, out_dir))

    write_json(out_dir / "summary.json", summaries)
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"outputs: {out_dir}")


if __name__ == "__main__":
    main()
