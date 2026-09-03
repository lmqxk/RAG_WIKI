r"""只重建 storage/wiki 派生层，不改动 SQLite 检索索引与 Qdrant 向量。

Wiki 锚点必须指向已入库的真实 chunk ID（随机生成的 ID 无法被检索层解析），
因此本脚本从数据库读取各文档已存储的 chunks，而不是用当前代码重新切块。

用法：
  backend\.venv\Scripts\python.exe scripts\rebuild_wiki.py            # 重建全部 READY 文档
  backend\.venv\Scripts\python.exe scripts\rebuild_wiki.py --document-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from backend.config import get_settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.domain import PageBlock, ParsedDocument  # noqa: E402
from backend.providers import ChatProvider  # noqa: E402
from backend.repository import Repository  # noqa: E402
from backend.wiki import WikiManager  # noqa: E402


def load_parsed(parsed_path: Path) -> ParsedDocument | None:
    normalized_path = parsed_path / "normalized.json"
    if not normalized_path.exists():
        return None
    payload = json.loads(normalized_path.read_text(encoding="utf-8"))
    blocks = [
        PageBlock(
            page=int(block["page"]),
            text=str(block["text"]),
            block_type=str(block.get("type", "text")),
            bbox=[float(value) for value in block.get("bbox", [])],
            level=block.get("level"),
            printed_page=block.get("printed_page"),
            images=list(block.get("images", [])),
        )
        for block in payload["blocks"]
    ]
    markdown_path = parsed_path / "document.md"
    return ParsedDocument(
        pages=int(payload.get("pages", 0)),
        blocks=blocks,
        markdown=markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else "",
        parser_name=str(payload.get("parser_name", "unknown")),
        needs_ocr=bool(payload.get("needs_ocr", False)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 Wiki 派生知识层")
    parser.add_argument("--document-id", default=None, help="只重建指定文档")
    args = parser.parse_args()

    settings = get_settings()
    repository = Repository(Database(settings.sqlite_path))
    chat = ChatProvider(settings)
    wiki = WikiManager(settings, chat)

    documents = [
        document
        for document in repository.list_documents()
        if document.get("parsed_path")
        and (args.document_id is None or str(document["id"]) == args.document_id)
    ]
    if not documents:
        print("没有可重建的文档（需要已有 parsed 解析产物）。")
        return 1

    failed = 0
    for document in documents:
        document_id = str(document["id"])
        title = document.get("title") or document.get("filename") or document_id
        parsed = load_parsed(Path(str(document["parsed_path"])))
        if parsed is None:
            print(f"[skip] {title}: normalized.json 不存在")
            failed += 1
            continue
        chunks = repository.document_chunks(document_id)
        if not chunks:
            print(f"[skip] {title}: 数据库中没有已入库的 chunks，请先走正常解析入库流程")
            failed += 1
            continue
        try:
            wiki.sync_document(document, parsed, chunks, event="reindex")
            metadata = wiki.metadata_dir / f"{document_id}.json"
            analysis = json.loads(metadata.read_text(encoding="utf-8"))["analysis"]
            concepts = analysis.get("concepts", [])
            generation = analysis.get("generation")
            print(
                f"[ok] {title}: {len(chunks)} chunks, "
                f"{len(concepts)} concepts (generation={generation})"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {title}: {exc}")
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
