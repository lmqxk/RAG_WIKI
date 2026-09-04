r"""用当前配置的 Embedding 后端重建 Qdrant 向量索引。

chunk ID 直接复用数据库已存储的记录（不重新切块），wiki 锚点与引用不受影响；
换 embedding 模型或从哈希降级恢复到真实语义向量时使用本脚本。

用法：
  backend\.venv\Scripts\python.exe scripts\rebuild_vectors.py            # 重建全部文档
  backend\.venv\Scripts\python.exe scripts\rebuild_vectors.py --document-id <id>
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from backend.db import Database
from backend.providers import EmbeddingProvider
from backend.repository import Repository
from backend.vector_index import VectorIndex

from backend.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 Qdrant 向量索引")
    parser.add_argument("--document-id", default=None, help="只重建指定文档")
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings.sqlite_path)
    database.initialize()
    repository = Repository(database)
    embeddings = EmbeddingProvider(settings)
    if not embeddings.configured:
        print("[abort] Embedding 语义后端未启用，请检查 RAG_EMBEDDING_* 配置")
        return 1
    index = VectorIndex(settings, embeddings)

    documents = repository.list_documents()
    if args.document_id:
        documents = [doc for doc in documents if str(doc["id"]) == args.document_id]
        if not documents:
            print(f"[abort] 文档不存在：{args.document_id}")
            return 1

    started = time.time()
    total = 0
    for doc in documents:
        document_id = str(doc["id"])
        if doc["status"] != "READY":
            print(f"[skip] {doc['title']}（状态 {doc['status']}）")
            continue
        chunks = repository.document_chunks(document_id)
        index.replace_document(document_id, chunks)
        total += len(chunks)
        print(f"[ok] {doc['title']}: {len(chunks)} chunks")

    print(f"完成：{len(documents)} 个文档 / {total} 个向量，耗时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
