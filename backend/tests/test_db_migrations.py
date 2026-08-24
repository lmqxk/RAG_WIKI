"""验证单机 SQLite 历史数据会无损迁移到默认组织。"""

import sqlite3
from pathlib import Path

from backend.db import Database


def test_initialize_backfills_legacy_documents_into_default_organization(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE, title TEXT NOT NULL, status TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0, file_size INTEGER NOT NULL,
                needs_ocr INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents (
                id, filename, stored_path, sha256, title, status, file_size, created_at, updated_at
            )
            VALUES ('doc-1', 'legacy.pdf', 'legacy.pdf', 'hash', '旧规范', 'READY', 1, 't', 't')
            """
        )

    Database(path).initialize()

    with sqlite3.connect(path) as connection:
        organization = connection.execute(
            "SELECT name FROM organizations WHERE id = 'default-org'"
        ).fetchone()
        document = connection.execute(
            "SELECT organization_id FROM documents WHERE id = 'doc-1'"
        ).fetchone()

    assert organization == ("默认组织",)
    assert document == ("default-org",)
