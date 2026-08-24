"""数据仓储模块：统一管理文档、任务、文本块、全文索引和检索结果持久化。

使用 QueryEngine 适配 SQLite 和 PostgreSQL 双后端。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from .auth import hash_password
from .db import utc_now
from .domain import Chunk, SearchHit
from .events import EVENT_JOB_UPDATED, EventBus
from .query_engine import DEFAULT_ORGANIZATION_ID, QueryEngine, create_query_engine

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


def row_to_document(row: object) -> dict[str, object]:
    if isinstance(row, dict):
        result = dict(row)
    else:
        result = dict(row)  # sqlite3.Row
    result["needs_ocr"] = bool(result.get("needs_ocr", False))
    result["error"] = summarize_error(result.get("error"))
    result["raw_error"] = result.get("error")
    return result


class Repository:
    def __init__(self, database=None, query_engine: QueryEngine | None = None, event_bus: EventBus | None = None) -> None:
        """使用 database 或预先创建的 query_engine。

        兼容旧代码：传 database 时自动创建 query_engine。
        """
        if query_engine is not None:
            self.engine = query_engine
        elif database is not None:
            self.engine = create_query_engine(database)
        else:
            raise ValueError("必须提供 database 或 query_engine")
        self._event_bus = event_bus

    @property
    def database(self):
        """兼容旧引用的属性。"""
        return self.engine.database if hasattr(self.engine, "database") else None

    def create_user(self, username: str, password: str) -> dict[str, object]:
        from uuid import uuid4

        user_id = str(uuid4())
        with self.engine.transaction() as txn:
            txn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, hash_password(password), utc_now()),
            )
        user = self.get_user_by_username(username)
        if user is None:
            raise RuntimeError("用户创建失败")
        return user

    def get_user_by_username(self, username: str) -> dict[str, object] | None:
        return self.engine.fetchone(
            "SELECT * FROM users WHERE username = ?", (username,)
        )

    def ensure_bootstrap_admin(self, username: str, password: str) -> None:
        """仅在用户不存在时创建默认组织管理员，已有用户的密码不被覆盖。"""

        user = self.get_user_by_username(username)
        if user is None:
            user = self.create_user(username, password)
        with self.engine.transaction() as txn:
            txn.execute(
                """
                INSERT OR IGNORE INTO organization_members (
                    organization_id, user_id, role, created_at
                )
                VALUES (?, ?, 'admin', ?)
                """,
                (DEFAULT_ORGANIZATION_ID, str(user["id"]), utc_now()),
            )

    def get_user_by_id(self, user_id: str) -> dict[str, object] | None:
        return self.engine.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_membership(self, organization_id: str, user_id: str) -> dict[str, object] | None:
        return self.engine.fetchone(
            """
            SELECT organization_id, user_id, role
            FROM organization_members
            WHERE organization_id = ? AND user_id = ?
            """,
            (organization_id, user_id),
        )

    def list_organizations(self) -> list[dict[str, object]]:
        return self.engine.fetchall("SELECT * FROM organizations ORDER BY created_at")

    def create_organization(self, name: str) -> dict[str, object]:
        from uuid import uuid4

        org_id = str(uuid4())
        with self.engine.transaction() as txn:
            txn.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
                (org_id, name, utc_now()),
            )
        return self.get_organization(org_id)

    def get_organization(self, organization_id: str) -> dict[str, object] | None:
        return self.engine.fetchone(
            "SELECT * FROM organizations WHERE id = ?", (organization_id,)
        )

    def list_members(
        self,
        organization_id: str,
    ) -> list[dict[str, object]]:
        return self.engine.fetchall(
            """
            SELECT om.organization_id, om.user_id, om.role, om.created_at,
                   u.username
            FROM organization_members om
            JOIN users u ON u.id = om.user_id
            WHERE om.organization_id = ?
            ORDER BY om.created_at
            """,
            (organization_id,),
        )

    def add_member(
        self,
        organization_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, object] | None:
        with self.engine.transaction() as txn:
            txn.execute(
                """
                INSERT OR IGNORE INTO organization_members (
                    organization_id, user_id, role, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (organization_id, user_id, role, utc_now()),
            )
        return self.get_membership(organization_id, user_id)

    def remove_member(self, organization_id: str, user_id: str) -> bool:
        with self.engine.transaction() as txn:
            cursor = txn.execute(
                "DELETE FROM organization_members WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            )
        return cursor.rowcount > 0

    def update_member_role(
        self,
        organization_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, object] | None:
        with self.engine.transaction() as txn:
            txn.execute(
                "UPDATE organization_members SET role = ? WHERE organization_id = ? AND user_id = ?",
                (role, organization_id, user_id),
            )
        return self.get_membership(organization_id, user_id)

    def create_audit_log(
        self,
        *,
        organization_id: str,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        detail: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        from uuid import uuid4

        log_id = str(uuid4())
        with self.engine.transaction() as txn:
            txn.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, user_id, action, resource_type,
                    resource_id, detail, ip_address, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (log_id, organization_id, user_id, action, resource_type,
                 resource_id, detail, ip_address, utc_now()),
            )

    def list_audit_logs(
        self,
        organization_id: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self.engine.fetchall(
            """
            SELECT al.*, u.username
            FROM audit_logs al
            JOIN users u ON u.id = al.user_id
            WHERE al.organization_id = ?
            ORDER BY al.created_at DESC
            LIMIT ?
            """,
            (organization_id, limit),
        )

    def list_documents(
        self,
        organization_id: str = DEFAULT_ORGANIZATION_ID,
    ) -> list[dict[str, object]]:
        rows = self.engine.fetchall(
            "SELECT * FROM documents WHERE organization_id = ? ORDER BY created_at DESC",
            (organization_id,),
        )
        return [row_to_document(row) for row in rows]

    def get_document(
        self,
        document_id: str,
        organization_id: str | None = None,
    ) -> dict[str, object] | None:
        """获取文档；提供 organization_id 时校验组织归属，否则仅按主键查询。"""
        if organization_id is not None:
            row = self.engine.fetchone(
                "SELECT * FROM documents WHERE id = ? AND organization_id = ?",
                (document_id, organization_id),
            )
        else:
            row = self.engine.fetchone(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            )
        return row_to_document(row) if row else None

    def get_document_by_hash(
        self,
        sha256: str,
        organization_id: str | None = None,
    ) -> dict[str, object] | None:
        """按哈希查找文档；提供 organization_id 时限定组织范围。"""
        if organization_id is not None:
            row = self.engine.fetchone(
                "SELECT * FROM documents WHERE sha256 = ? AND organization_id = ?",
                (sha256, organization_id),
            )
        else:
            row = self.engine.fetchone(
                "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
            )
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
        organization_id: str = DEFAULT_ORGANIZATION_ID,
    ) -> dict[str, object]:
        now = utc_now()
        with self.engine.transaction() as txn:
            txn.execute(
                """
                INSERT INTO documents (
                    id, organization_id, filename, stored_path, sha256, title, standard_no, version,
                    status, page_count, file_size, needs_ocr, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?, 0, ?, ?)
                """,
                (
                    document_id,
                    organization_id,
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
        document = self.get_document(document_id, organization_id)
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
        with self.engine.transaction() as txn:
            txn.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",  # noqa: S608
                parameters,
            )

    def create_job(self, job_id: str, document_id: str) -> dict[str, object]:
        now = utc_now()
        with self.engine.transaction() as txn:
            txn.execute(
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

    def create_job_if_idle(
        self,
        job_id: str,
        document_id: str,
    ) -> tuple[dict[str, object], bool]:
        """原子地复用活动任务，或为文档创建一个新任务。"""

        now = utc_now()
        with self.engine.transaction() as txn:
            active = txn.execute(
                """
                SELECT * FROM jobs
                WHERE document_id = ? AND status IN ('QUEUED', 'RUNNING')
                ORDER BY created_at DESC LIMIT 1
                """,
                (document_id,),
            )
            # SQLite: cursor has .fetchone()
            # PostgreSQL: result has .first()
            active_row = (
                active.fetchone()
                if hasattr(active, "fetchone")
                else active.first()
            )
            if active_row is not None:
                result = _row_to_dict(active_row)
                result["error"] = summarize_error(result.get("error"))
                result["raw_error"] = result.get("error")
                return result, False
            txn.execute(
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
        return job, True

    def get_job(
        self,
        job_id: str,
        organization_id: str | None = None,
    ) -> dict[str, object] | None:
        if organization_id is not None:
            row = self.engine.fetchone(
                """
                SELECT j.* FROM jobs j
                JOIN documents d ON d.id = j.document_id
                WHERE j.id = ? AND d.organization_id = ?
                """,
                (job_id, organization_id),
            )
        else:
            row = self.engine.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = result.get("error")
        return result

    def latest_job_for_document(self, document_id: str) -> dict[str, object] | None:
        row = self.engine.fetchone(
            "SELECT * FROM jobs WHERE document_id = ? ORDER BY created_at DESC LIMIT 1",
            (document_id,),
        )
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = result.get("error")
        return result

    def active_job_for_document(self, document_id: str) -> dict[str, object] | None:
        row = self.engine.fetchone(
            """
            SELECT * FROM jobs
            WHERE document_id = ? AND status IN ('QUEUED', 'RUNNING')
            ORDER BY created_at DESC LIMIT 1
            """,
            (document_id,),
        )
        if row is None:
            return None
        result = dict(row)
        result["error"] = summarize_error(result.get("error"))
        result["raw_error"] = result.get("error")
        return result

    def pending_jobs(self) -> list[dict[str, object]]:
        with self.engine.transaction() as txn:
            txn.execute(
                """
                UPDATE jobs SET status = 'QUEUED', stage = 'queued',
                    message = '服务重启，任务重新排队', updated_at = ?
                WHERE status = 'RUNNING'
                """,
                (utc_now(),),
            )
            rows = txn.execute(
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
        with self.engine.transaction() as txn:
            txn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",  # noqa: S608
                [*values.values(), job_id],
            )
        # 发布事件到事件总线
        self._publish_job_event(job_id, values)

    def _publish_job_event(self, job_id: str, values: dict[str, object]) -> None:
        """发布任务更新事件。"""
        if self._event_bus is None:
            return
        try:
            import asyncio

            # 从运行中的事件循环发布事件（如果存在）
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(
                        self._event_bus.publish(
                            "jobs",
                            EVENT_JOB_UPDATED,
                            {"job_id": job_id, **values},
                        )
                    )
            except RuntimeError:
                pass  # 没有运行中的事件循环，跳过
        except Exception:
            pass  # 事件发布失败不应影响主流程

    def replace_chunks(self, document_id: str, chunks: Sequence[Chunk]) -> None:
        self.engine.replace_chunks(document_id, chunks)

    def get_chunks(
        self,
        chunk_ids: Sequence[str],
        organization_id: str | None = None,
    ) -> list[SearchHit]:
        return self.engine.get_chunks(chunk_ids, organization_id)

    def bm25_search(
        self,
        query: str,
        *,
        document_ids: Sequence[str] | None,
        organization_id: str | None = None,
        limit: int,
    ) -> list[SearchHit]:
        return self.engine.bm25_search(
            query,
            document_ids=document_ids,
            organization_id=organization_id,
            limit=limit,
        )

    def exact_clause_search(
        self,
        clause_no: str,
        *,
        document_ids: Sequence[str] | None,
        organization_id: str | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        return self.engine.exact_clause_search(
            clause_no,
            document_ids=document_ids,
            organization_id=organization_id,
            limit=limit,
        )
