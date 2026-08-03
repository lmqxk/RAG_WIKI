r"""查看文档切块样式。

用法：
  backend\.venv\Scripts\python.exe scripts\show_chunks.py --document-id <id>
  backend\.venv\Scripts\python.exe scripts\show_chunks.py --parsed-dir storage\parsed\<id> --fresh
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from backend.chunking import build_chunks  # noqa: E402
from backend.domain import Chunk, PageBlock  # noqa: E402


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def print_chunk(chunk: Chunk, *, text_limit: int) -> None:
    print("=" * 88)
    print(f"#{chunk.ordinal}")
    print(f"id          : {chunk.id}")
    print(f"source_type : {chunk.source_type}")
    print(f"page        : {chunk.page_start}-{chunk.page_end}")
    print(f"printed_page: {chunk.printed_page or '-'}")
    print(f"chapter_path: {chunk.chapter_path or '-'}")
    print(f"clause_no   : {chunk.clause_no or '-'}")
    print(f"parent_id   : {chunk.parent_id or '-'}")
    print(f"text_len    : {len(chunk.text)}")
    print("text:")
    print(shorten(chunk.text, text_limit))


def load_db_chunks(document_id: str) -> list[Chunk]:
    db_path = ROOT / "storage" / "rag.db"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, document_id, ordinal, chapter_path, clause_no, text,
                   page_start, page_end, printed_page, source_type, parent_id
            FROM chunks
            WHERE document_id = ?
            ORDER BY ordinal
            """,
            (document_id,),
        ).fetchall()
    return [
        Chunk(
            id=row["id"],
            document_id=row["document_id"],
            ordinal=row["ordinal"],
            chapter_path=row["chapter_path"],
            clause_no=row["clause_no"],
            text=row["text"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            printed_page=row["printed_page"],
            source_type=row["source_type"],
            parent_id=row["parent_id"],
        )
        for row in rows
    ]


def load_fresh_chunks(parsed_dir: Path, document_id: str | None) -> list[Chunk]:
    normalized_path = parsed_dir / "normalized.json"
    payload = json.loads(normalized_path.read_text(encoding="utf-8"))
    resolved_document_id = document_id or parsed_dir.name
    blocks = [
        PageBlock(
            page=int(block["page"]),
            text=str(block["text"]),
            block_type=str(block.get("type", "text")),
            bbox=[float(value) for value in block.get("bbox", [])],
            level=block.get("level"),
            printed_page=block.get("printed_page"),
        )
        for block in payload["blocks"]
    ]
    return build_chunks(resolved_document_id, blocks)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="查看某份文档的 chunk 切块样式")
    parser.add_argument("--document-id", help="文档 ID；默认从 storage/rag.db 读取已入库 chunk")
    parser.add_argument("--parsed-dir", type=Path, help="解析结果目录，包含 normalized.json")
    parser.add_argument("--fresh", action="store_true", help="从 normalized.json 用当前代码重新切块预览")
    parser.add_argument("--contains", help="只显示包含该关键词的 chunk")
    parser.add_argument("--limit", type=int, default=30, help="最多显示多少个 chunk")
    parser.add_argument("--text-limit", type=int, default=800, help="每个 chunk 最多显示多少字符")
    args = parser.parse_args()

    if args.fresh:
        if not args.parsed_dir:
            parser.error("--fresh 需要同时传 --parsed-dir")
        chunks = load_fresh_chunks(args.parsed_dir, args.document_id)
        mode = "fresh normalized.json preview"
    else:
        if not args.document_id:
            parser.error("读取数据库 chunk 需要传 --document-id")
        chunks = load_db_chunks(args.document_id)
        mode = "stored database chunks"

    if args.contains:
        chunks = [chunk for chunk in chunks if args.contains in chunk.text]

    print(f"mode   : {mode}")
    print(f"count  : {len(chunks)}")
    print(f"filter : {args.contains or '-'}")
    print()

    for chunk in chunks[: args.limit]:
        print_chunk(chunk, text_limit=args.text_limit)
    if len(chunks) > args.limit:
        print("=" * 88)
        print(f"还有 {len(chunks) - args.limit} 个 chunk 未显示，可调大 --limit。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
