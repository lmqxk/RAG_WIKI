"""数据库基础模块：负责 SQLite 连接、事务管理、表初始化和时间序列化。

支持两种模式：
- SQLite（默认）：使用 sqlite3 模块 + FTS5，适合开发与单机部署
- SQLAlchemy（设置 database_url 时）：支持 PostgreSQL，适合生产部署
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AuditLog, Chunk, Document, Job, Organization, OrganizationMember, User, init_db

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_members (
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_organization_members_user ON organization_members(user_id);

INSERT OR IGNORE INTO organizations (id, name, created_at)
VALUES ('default-org', '默认组织', '1970-01-01T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'default-org' REFERENCES organizations(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    standard_no TEXT,
    version TEXT,
    status TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL,
    needs_ocr INTEGER NOT NULL DEFAULT 0,
    parser_name TEXT,
    parsed_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, sha256)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    ordinal INTEGER NOT NULL,
    chapter_path TEXT NOT NULL DEFAULT '',
    clause_no TEXT,
    text TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    printed_page TEXT,
    source_type TEXT NOT NULL DEFAULT 'text',
    parent_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    detail TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_org ON audit_logs(organization_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_jobs_document ON jobs(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_clause ON chunks(clause_no);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    document_id UNINDEXED,
    clause_no,
    chapter_path,
    terms,
    tokenize = 'unicode61'
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path, database_url: str | None = None) -> None:
        self.path = path
        self.database_url = database_url
        self._engine: Any = None
        self._session_factory: Any = None
        self._persistent_conn: sqlite3.Connection | None = None

    @property
    def use_sqlalchemy(self) -> bool:
        """是否使用 SQLAlchemy（PostgreSQL）模式。"""
        return bool(self.database_url) and not self.database_url.startswith("sqlite")

    @property
    def engine(self) -> Any:
        """获取 SQLAlchemy 引擎（仅 PostgreSQL 模式）。"""
        if self._engine is None and self.database_url:
            from .models import create_engine_from_settings

            self._engine = create_engine_from_settings(self.database_url)
        return self._engine

    @property
    def session_factory(self) -> Any:
        """获取 SQLAlchemy sessionmaker（仅 PostgreSQL 模式）。"""
        if self._session_factory is None and self.engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._session_factory = sessionmaker(bind=self.engine)
        return self._session_factory

    def connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._persistent_conn is None:
                self._persistent_conn = sqlite3.connect(
                    "file::memory:?cache=shared", timeout=30, check_same_thread=False, uri=True
                )
                self._persistent_conn.row_factory = sqlite3.Row
                self._persistent_conn.execute("PRAGMA foreign_keys = ON")
            return self._persistent_conn
        connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        if self.use_sqlalchemy:
            # PostgreSQL 模式：使用 SQLAlchemy ORM 创建表
            init_db(self.engine)
            # 确保默认组织存在
            self._ensure_default_organization_sa()
        else:
            # SQLite 模式：使用原生 SQL 脚本（含 FTS5）
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.executescript(SCHEMA)
                self._migrate_legacy_documents(connection)

    def _ensure_default_organization_sa(self) -> None:
        """确保 PostgreSQL 中存在默认组织。"""
        from sqlalchemy import text

        with self.engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM organizations WHERE id = 'default-org'")
            )
            if not result.scalar():
                conn.execute(
                    text(
                        "INSERT INTO organizations (id, name, created_at) "
                        "VALUES ('default-org', '默认组织', '1970-01-01T00:00:00+00:00')"
                    )
                )
                conn.commit()

    @staticmethod
    def _migrate_legacy_documents(connection: sqlite3.Connection) -> None:
        """为已有单机库添加组织归属，并无损归入默认组织。"""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "organization_id" not in columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN organization_id "
                "TEXT NOT NULL DEFAULT 'default-org'"
            )
        connection.execute(
            "UPDATE documents SET organization_id = 'default-org' "
            "WHERE organization_id IS NULL OR organization_id = ''"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_organization ON documents(organization_id)"
        )

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
