"""数据仓储模块：统一管理文档、任务、文本块、全文索引和检索结果持久化。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .db import Database, utc_now
from .domain import Chunk, SearchHit

ASCII_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|\d+(?:\.\d+)*")
HAN_RE = re.compile(r"[\u3400-\u9fff]+")


def summarize_error(error: object) -> str | None:
    """把后台 traceback 压缩成适合前端展示的短错误。"""

    if error is None:
        return None
    text = str(error).strip()
    if not text:
        return None
    lowered = text.lower()
    if "cuda error: no kernel image is available for execution on the device" in lowered:
        return (
            "CUDA 与当前 PyTorch wheel 不兼容：当前显卡算力不在 torch 支持架构内。"
            "请更换支持该显卡的 torch CUDA wheel，或临时使用 CPU/其他解析管线。"
        )
    if "active job" in lowered:
        return text.splitlines()[0][:300]
    if "没有可复用的结构化解析结果" in text or "结构化解析结果不存在" in text:
        return text.splitlines()[0][:300]
    if "pdf already exists in another document" in lowered:
        return "该 PDF 已存在于另一份文档中。"
    if "exceeded" in lowered or "超过当前" in text:
        return text.splitlines()[0][:300]
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
    first_line = first_line.replace("�", "")
    if "Traceback" in first_line:
        for line in text.splitlines():
            clean = line.strip().replace("�", "")
            if clean and not clean.startswith("File ") and "Traceback" not in clean:
                first_line = clean
                break
    return first_line[:300]


def search_terms(text: str) -> str:
    """生成适合 SQLite FTS5 的中英文检索词，中文使用二元字串。"""

    terms = [term.lower() for term in ASCII_TERM_RE.findall(text)]
    for sequence in HAN_RE.findall(text):
        if len(sequence) == 1:
            terms.append(sequence)
        else:
            terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return " ".join(terms)


def row_to_document(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["needs_ocr"] = bool(result["needs_ocr"])
    result["error"] = summarize_error(result.get("error"))
    result["raw_error"] = row["error"] if "error" in row.keys() else None
    return result


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_documents(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return [row_to_document(row) for row in rows]

    def get_document(self, document_id: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        return row_to_document(row) if row else None

    def get_document_by_hash(self, sha256: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return row_to_document(row) if row else None

    def create_document(
        self,
        *,
        document_id: str,
        filename: str,
        stored_path: Path,
        sha256: str,
        title: str,
        standard_no: str | None,
        version: str | None,
        file_size: int,
    ) -> dict[str, object]:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    id, filename, stored_path, sha256, title, standard_no, version,
                    status, page_count, file_size, needs_ocr, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?, 0, ?, ?)
                """,
                (
                    document_id,
                    filename,
                    str(stored_path),
                    sha256,
                    title,
                    standard_no,
                    version,
                    file_size,
                    now,
                    now,
                ),
            )
        document = self.get_document(document_id)
        if document is None:
            raise RuntimeError("文档创建失败")
        return document

    def update_document(self, document_id: str, **values: object) -> None:
        if not values:
            return
        allowed = {
            "filename",
            "stored_path",
            "sha256",
            "title",
            "standard_no",
            "version",
            "status",
            "page_count",
            "file_size",
            "needs_ocr",
            "parser_name",
            "parsed_path",
            "error",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段: {sorted(unknown)}")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = [*values.values(), document_id]
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",  # noqa: S608
                parameters,
            )

    def create_job(self, job_id: str, document_id: str) -> dict[str, object]:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, document_id, status, stage, progress, message, created_at, updated_at
                ) VALUES (?, ?, 'QUEUED', 'queued', 0, '等待解析', ?, ?)
                """,
                (job_id, document_id, now, now),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("任务创建失败")
        return job

    def get_job(self, job_id: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = row["error"]
        return result

    def latest_job_for_document(self, document_id: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs WHERE document_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = row["error"]
        return result

    def active_job_for_document(self, document_id: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE document_id = ? AND status IN ('QUEUED', 'RUNNING')
                ORDER BY created_at DESC LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = row["error"]
        return result

    def pending_jobs(self) -> list[dict[str, object]]:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'QUEUED', stage = 'queued',
                    message = '服务重启，任务重新排队', updated_at = ?
                WHERE status = 'RUNNING'
                """,
                (utc_now(),),
            )
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'QUEUED' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, object] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if stage is not None:
            values["stage"] = stage
        if progress is not None:
            values["progress"] = max(0, min(100, progress))
        if message is not None:
            values["message"] = message
        if error is not None:
            values["error"] = error
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",  # noqa: S608
                [*values.values(), job_id],
            )

    def replace_chunks(self, document_id: str, chunks: Sequence[Chunk]) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()
            for row in existing:
                connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (row["id"],))
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO chunks (
                        id, document_id, ordinal, chapter_path, clause_no, text,
                        page_start, page_end, printed_page, source_type, parent_id, created_at
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
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chunks_fts (
                        chunk_id, document_id, clause_no, chapter_path, terms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.clause_no or "",
                        chunk.chapter_path,
                        search_terms(
                            " ".join(
                                [
                                    chunk.clause_no or "",
                                    chunk.chapter_path,
                                    chunk.text,
                                ]
                            )
                        ),
                    ),
                )

    def get_chunks(self, chunk_ids: Sequence[str]) -> list[SearchHit]:
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                  AND d.status = 'READY'
                """,  # noqa: S608
                list(chunk_ids),
            ).fetchall()
        by_id = {
            row["id"]: SearchHit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                text=row["text"],
                clause_no=row["clause_no"],
                chapter_path=row["chapter_path"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                printed_page=row["printed_page"],
                document_title=row["document_title"],
                standard_no=row["standard_no"],
                version=row["version"],
                score=0,
                source="lookup",
                content_type=row["source_type"],
            )
            for row in rows
        }
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    def bm25_search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None,
        limit: int,
    ) -> list[SearchHit]:
        terms = search_terms(query).split()
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term}"' for term in dict.fromkeys(terms[:50]))
        parameters: list[object] = [fts_query]
        document_filter = ""
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            document_filter = f"AND c.document_id IN ({placeholders})"
            parameters.extend(document_ids)
        parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version,
                       bm25(chunks_fts, 0, 0, 6, 2, 1) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ? {document_filter}
                  AND d.status = 'READY'
                ORDER BY rank
                LIMIT ?
                """,  # noqa: S608
                parameters,
            ).fetchall()
        return [
            SearchHit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                text=row["text"],
                clause_no=row["clause_no"],
                chapter_path=row["chapter_path"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                printed_page=row["printed_page"],
                document_title=row["document_title"],
                standard_no=row["standard_no"],
                version=row["version"],
                score=1 / (1 + max(float(row["rank"]), 0)),
                source="bm25",
                content_type=row["source_type"],
            )
            for row in rows
        ]

    def exact_clause_search(
        self,
        clause_no: str,
        *,
        document_ids: Sequence[str] | None,
        limit: int = 10,
    ) -> list[SearchHit]:
        parameters: list[object] = [clause_no, f"%{clause_no}%"]
        document_filter = ""
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            document_filter = f"AND c.document_id IN ({placeholders})"
            parameters.extend(document_ids)
        parameters.append(limit * 5)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.title AS document_title, d.standard_no, d.version
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE (c.clause_no = ? OR c.text LIKE ?)
                    {document_filter} AND d.status = 'READY'
                ORDER BY
                    CASE
                        WHEN c.source_type = 'normative' THEN 0
                        WHEN c.source_type = 'normative_table' THEN 1
                        ELSE 2
                    END,
                    d.version DESC
                LIMIT ?
                """,  # noqa: S608
                parameters,
            ).fetchall()
        direct_clause = re.compile(
            rf"(?:^|\s){re.escape(clause_no)}\s*[\u3400-\u9fff]"
        )
        rows = sorted(
            rows,
            key=lambda row: (
                0
                if row["clause_no"] == clause_no
                else 1
                if direct_clause.search(row["text"])
                else 2,
                0 if row["source_type"].startswith("normative") else 1,
                row["page_start"],
            ),
        )[:limit]
        return [
            SearchHit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                text=row["text"],
                clause_no=(
                    row["clause_no"]
                    or (clause_no if direct_clause.search(row["text"]) else None)
                ),
                chapter_path=row["chapter_path"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                printed_page=row["printed_page"],
                document_title=row["document_title"],
                standard_no=row["standard_no"],
                version=row["version"],
                score=1.0,
                source=(
                    "exact"
                    if row["clause_no"] == clause_no
                    or direct_clause.search(row["text"])
                    else "clause_reference"
                ),
                content_type=row["source_type"],
            )
            for row in rows
        ]
