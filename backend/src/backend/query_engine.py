"""数据库查询引擎抽象层：统一 SQLite 和 PostgreSQL 的查询接口。

SQLite 模式使用原生 sqlite3 模块 + FTS5 全文检索。
PostgreSQL 模式使用 SQLAlchemy ORM + tsvector 全文检索。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from .db import Database, utc_now
from .domain import Chunk, SearchHit

ASCII_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|\d+(?:\.\d+)*")
HAN_RE = re.compile(r"[\u3400-\u9fff]+")
DEFAULT_ORGANIZATION_ID = "default-org"


# ── 行转 dict 工具 ──────────────────────────────────────────────────────


def _row_to_dict(row: Any) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, object]]:
    return [_row_to_dict(row) for row in rows]


# ── 全文检索词处理 ────────────────────────────────────────────────────────


def search_terms(text: str) -> str:
    """生成适合全文检索的中英文检索词，中文使用二元字串。"""
    terms = [term.lower() for term in ASCII_TERM_RE.findall(text)]
    for sequence in HAN_RE.findall(text):
        if len(sequence) == 1:
            terms.append(sequence)
        else:
            terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return " ".join(terms)


def _summarize_error(error: object) -> str | None:
    """压缩异常信息。"""
    if error is None:
        return None
    text = str(error).strip()
    if not text:
        return None
    lowered = text.lower()
    if "cuda error: no kernel image is available for execution on the device" in lowered:
        return (
            "CUDA 与当前 PyTorch wheel 不兼容。"
            "请更换支持该显卡的 torch CUDA wheel，或临时使用 CPU。"
        )
    if "active job" in lowered:
        return text.splitlines()[0][:300]
    if "pdf already exists" in lowered:
        return "该 PDF 已存在于另一份文档中。"
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
    first_line = first_line.replace("", "")
    if "Traceback" in first_line:
        for line in text.splitlines():
            clean = line.strip().replace("", "")
            if clean and not clean.startswith("File ") and "Traceback" not in clean:
                first_line = clean
                break
    return first_line[:300]


# ── 统一事务上下文 ────────────────────────────────────────────────────────


class _SQLiteTransaction:
    """SQLite 事务包装：提供 execute() 兼容接口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._closed = False

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True


class _PGTransaction:
    """PostgreSQL 事务包装：将 SQLite 风格 ? 参数转为 SQLAlchemy 命名参数。"""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._closed = False

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        from sqlalchemy import text

        if params:
            named = {str(i): v for i, v in enumerate(params)}
            return self._session.execute(text(sql), named)
        return self._session.execute(text(sql))

    def close(self) -> None:
        if not self._closed:
            self._session.close()
            self._closed = True


# ── 抽象协议 ──────────────────────────────────────────────────────────────


class QueryEngine(Protocol):
    """统一查询引擎协议。"""

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        """执行 SQL 并返回游标/结果。"""
        ...

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> dict[str, object] | None:
        """查询单行，返回 dict 或 None。"""
        ...

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, object]]:
        """查询多行，返回 list[dict]。"""
        ...

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """事务上下文管理器。"""
        ...

    def bm25_search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """全文检索。"""
        ...

    def exact_clause_search(
        self,
        clause_no: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        """精确条款检索。"""
        ...

    def replace_chunks(
        self,
        document_id: str,
        chunks: Sequence[Chunk],
    ) -> None:
        """替换文档的全部文本块，重建全文索引。"""
        ...

    def get_chunks(
        self,
        chunk_ids: Sequence[str],
        organization_id: str | None = None,
    ) -> list[SearchHit]:
        """按 ID 批量获取文本块。"""
        ...


# ── SQLite 实现 ──────────────────────────────────────────────────────────


class SQLiteEngine:
    """SQLite 查询引擎：使用原生 sqlite3 + FTS5。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _conn(self) -> sqlite3.Connection:
        return self.database.connect()

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        conn = self._conn()
        try:
            return conn.execute(sql, params)
        finally:
            conn.close()

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> dict[str, object] | None:
        conn = self._conn()
        try:
            row = conn.execute(sql, params).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, object]]:
        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[_SQLiteTransaction]:
        with self.database.transaction(immediate=True) as conn:
            yield _SQLiteTransaction(conn)

    def _row_to_search_hit(self, row: sqlite3.Row) -> SearchHit:
        data = _row_to_dict(row)
        return SearchHit(
            chunk_id=str(data.get("id", "")),
            document_id=str(data.get("document_id", "")),
            text=str(data.get("text", "")),
            clause_no=data.get("clause_no"),
            chapter_path=str(data.get("chapter_path", "")),
            page_start=int(data.get("page_start", 0)),
            page_end=int(data.get("page_end", 0)),
            printed_page=data.get("printed_page"),
            document_title=str(data.get("document_title", "")),
            standard_no=data.get("standard_no"),
            version=data.get("version"),
            score=float(data.get("score", 0) or 0),
            source=str(data.get("source", "hybrid")),
            content_type=str(data.get("source_type", "normative")),
        )

    def bm25_search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        terms = search_terms(query).split()
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term}"' for term in dict.fromkeys(terms[:50]))
        parameters: list[object] = [fts_query]
        doc_filter = ""
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            doc_filter = f"AND c.document_id IN ({placeholders})"
            parameters.extend(document_ids)
        org_filter = ""
        if organization_id is not None:
            org_filter = "AND d.organization_id = ?"
            parameters.append(organization_id)
        parameters.append(limit)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version,
                       bm25(chunks_fts, 0, 0, 6, 2, 1) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ? {doc_filter} {org_filter}
                  AND d.status = 'READY'
                ORDER BY score
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [self._row_to_search_hit(row) for row in rows]
        finally:
            conn.close()

    def exact_clause_search(
        self,
        clause_no: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        parameters: list[object] = [clause_no, f"%{clause_no}%"]
        doc_filter = ""
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            doc_filter = f"AND c.document_id IN ({placeholders})"
            parameters.extend(document_ids)
        org_filter = ""
        if organization_id is not None:
            org_filter = "AND d.organization_id = ?"
            parameters.append(organization_id)
        parameters.append(limit * 5)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE (c.clause_no = ? OR c.text LIKE ?)
                    {doc_filter} {org_filter} AND d.status = 'READY'
                ORDER BY
                    CASE WHEN c.source_type = 'normative' THEN 0
                         WHEN c.source_type = 'normative_table' THEN 1
                         ELSE 2
                    END, d.version DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            hits = []
            direct_clause = re.compile(
                r"(?<!\d)" + re.escape(clause_no) + r"(?!\d)"
            )
            for row in rows:
                hit = self._row_to_search_hit(row)
                hit.source = (
                    "exact" if row["clause_no"] == clause_no
                    or direct_clause.search(row["text"])
                    else "clause_reference"
                )
                hit.score = 1.0
                hits.append(hit)
            return hits
        finally:
            conn.close()

    def get_chunks(
        self,
        chunk_ids: Sequence[str],
        organization_id: str | None = None,
    ) -> list[SearchHit]:
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        org_filter = ""
        parameters: list[object] = list(chunk_ids)
        if organization_id is not None:
            org_filter = "AND d.organization_id = ?"
            parameters.append(organization_id)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders}) AND d.status = 'READY' {org_filter}
                """,
                parameters,
            ).fetchall()
            hits = []
            for row in rows:
                hit = self._row_to_search_hit(row)
                hit.score = 0.0
                hit.source = "dense"
                hits.append(hit)
            return hits
        finally:
            conn.close()

    def replace_chunks(
        self,
        document_id: str,
        chunks: Sequence[Chunk],
    ) -> None:
        with self.database.transaction() as conn:
            # 删除旧 chunks 和 FTS
            conn.execute("DELETE FROM chunks_fts WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            # 插入新 chunks
            for chunk in chunks:
                conn.execute(
                    """
                    INSERT INTO chunks (
                        id, document_id, ordinal, chapter_path, clause_no,
                        text, page_start, page_end, printed_page, source_type,
                        parent_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.chapter_path,
                        chunk.clause_no,
                        chunk.text,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.printed_page,
                        chunk.source_type,
                        chunk.parent_id,
                        utc_now(),
                    ),
                )
                # 插入 FTS5
                terms = search_terms(chunk.text)
                conn.execute(
                    """
                    INSERT INTO chunks_fts (chunk_id, document_id, clause_no, chapter_path, terms)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk.id, chunk.document_id, chunk.clause_no, chunk.chapter_path, terms),
                )


# ── PostgreSQL 实现 ──────────────────────────────────────────────────────


class PostgreSQLEngine:
    """PostgreSQL 查询引擎：使用 SQLAlchemy ORM + tsvector 全文检索。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._fts_config = "pg_catalog.simple"

    def _session(self):
        from sqlalchemy.orm import Session

        return Session(self.database.engine)

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        from sqlalchemy import text

        session = self._session()
        try:
            return session.execute(text(sql), dict(enumerate(params)) if params else {})
        finally:
            session.close()

    def fetchone(self, sql: str, params: Sequence[object] = ()) -> dict[str, object] | None:
        from sqlalchemy import text

        session = self._session()
        try:
            row = session.execute(text(sql), dict(enumerate(params)) if params else {}).first()
            return _row_to_dict(row) if row else None
        finally:
            session.close()

    def fetchall(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, object]]:
        from sqlalchemy import text

        session = self._session()
        try:
            rows = session.execute(text(sql), dict(enumerate(params)) if params else {}).all()
            return _rows_to_dicts(rows)
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Iterator[_PGTransaction]:
        from sqlalchemy.orm import Session

        session = Session(self.database.engine)
        try:
            yield _PGTransaction(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _row_to_search_hit(self, row: Any) -> SearchHit:
        d = _row_to_dict(row)
        return SearchHit(
            chunk_id=str(d.get("id", "")),
            document_id=str(d.get("document_id", "")),
            text=str(d.get("text", "")),
            clause_no=d.get("clause_no"),
            chapter_path=str(d.get("chapter_path", "")),
            page_start=int(d.get("page_start", 0)),
            page_end=int(d.get("page_end", 0)),
            printed_page=d.get("printed_page"),
            document_title=str(d.get("document_title", "")),
            standard_no=d.get("standard_no"),
            version=d.get("version"),
            score=float(d.get("score", 0) or 0),
            source=str(d.get("source", "hybrid")),
            content_type=str(d.get("source_type", "normative")),
        )

    def bm25_search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        from sqlalchemy import text

        terms = search_terms(query).split()
        if not terms:
            return []
        pg_query = " & ".join(terms[:50])
        params: dict[str, object] = {"query": pg_query, "limit": limit}
        filters = ["d.status = 'READY'"]
        if document_ids:
            filters.append("c.document_id = ANY(:document_ids)")
            params["document_ids"] = list(document_ids)
        if organization_id is not None:
            filters.append("d.organization_id = :org_id")
            params["org_id"] = organization_id
        where_clause = " AND ".join(filters)

        session = self._session()
        try:
            rows = session.execute(
                text(
                    f"""
                    SELECT c.*, d.title AS document_title, d.standard_no, d.version,
                           ts_rank(to_tsvector(:config, c.text), to_tsquery(:config, :query))
                           AS score
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE to_tsvector(:config, c.text) @@ to_tsquery(:config, :query)
                      AND {where_clause}
                    ORDER BY score DESC
                    LIMIT :limit
                    """
                ),
                {"config": self._fts_config, **params},
            ).all()
            return [self._row_to_search_hit(row) for row in rows]
        finally:
            session.close()

    def exact_clause_search(
        self,
        clause_no: str,
        *,
        document_ids: Sequence[str] | None = None,
        organization_id: str | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        from sqlalchemy import text

        params: dict[str, object] = {"clause_no": clause_no, "pattern": f"%{clause_no}%", "limit": limit * 5}
        filters = ["d.status = 'READY'"]
        if document_ids:
            filters.append("c.document_id = ANY(:document_ids)")
            params["document_ids"] = list(document_ids)
        if organization_id is not None:
            filters.append("d.organization_id = :org_id")
            params["org_id"] = organization_id
        where_clause = " AND ".join(filters)

        session = self._session()
        try:
            rows = session.execute(
                text(
                    f"""
                    SELECT c.*, d.title AS document_title, d.standard_no, d.version,
                           CASE WHEN c.source_type = 'normative' THEN 0
                                WHEN c.source_type = 'normative_table' THEN 1
                                ELSE 2 END AS sort_order
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE (c.clause_no = :clause_no OR c.text LIKE :pattern)
                      AND {where_clause}
                    ORDER BY sort_order, d.version DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).all()
            hits = []
            direct_clause = re.compile(r"(?<!\d)" + re.escape(clause_no) + r"(?!\d)")
            for row in rows:
                hit = self._row_to_search_hit(row)
                d = _row_to_dict(row)
                hit.source = (
                    "exact" if d.get("clause_no") == clause_no
                    or direct_clause.search(d.get("text", ""))
                    else "clause_reference"
                )
                hit.score = 1.0
                hits.append(hit)
            return hits
        finally:
            session.close()

    def get_chunks(
        self,
        chunk_ids: Sequence[str],
        organization_id: str | None = None,
    ) -> list[SearchHit]:
        if not chunk_ids:
            return []
        from sqlalchemy import text

        params: dict[str, object] = {"chunk_ids": list(chunk_ids)}
        org_filter = ""
        if organization_id is not None:
            org_filter = "AND d.organization_id = :org_id"
            params["org_id"] = organization_id

        session = self._session()
        try:
            rows = session.execute(
                text(
                    f"""
                    SELECT c.*, d.title AS document_title, d.standard_no, d.version
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.id = ANY(:chunk_ids) AND d.status = 'READY' {org_filter}
                    """
                ),
                params,
            ).all()
            hits = []
            for row in rows:
                hit = self._row_to_search_hit(row)
                hit.score = 0.0
                hit.source = "dense"
                hits.append(hit)
            return hits
        finally:
            session.close()

    def replace_chunks(
        self,
        document_id: str,
        chunks: Sequence[Chunk],
    ) -> None:
        with self.transaction() as txn:
            txn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            for chunk in chunks:
                txn.execute(
                    """
                    INSERT INTO chunks (
                        id, document_id, ordinal, chapter_path, clause_no,
                        text, page_start, page_end, printed_page, source_type,
                        parent_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.chapter_path,
                        chunk.clause_no,
                        chunk.text,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.printed_page,
                        chunk.source_type,
                        chunk.parent_id,
                        utc_now(),
                    ),
                )


# ── 工厂函数 ──────────────────────────────────────────────────────────────


def create_query_engine(database: Database) -> QueryEngine:
    """根据数据库配置创建合适的查询引擎。"""
    if database.use_sqlalchemy:
        return PostgreSQLEngine(database)
    return SQLiteEngine(database)