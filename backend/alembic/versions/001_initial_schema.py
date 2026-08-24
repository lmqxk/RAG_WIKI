"""initial_schema: 创建所有基础表。

Revision ID: 001
Revises:
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # organizations
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.execute(
        "INSERT INTO organizations (id, name, created_at) "
        "VALUES ('default-org', '默认组织', '1970-01-01T00:00:00+00:00')"
    )

    # users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )

    # organization_members
    op.create_table(
        "organization_members",
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index("idx_organization_members_user", "organization_members", ["user_id"])

    # documents
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"),
                  nullable=False, server_default="default-org"),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("stored_path", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("standard_no", sa.String(100), nullable=True),
        sa.Column("version", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("needs_ocr", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parser_name", sa.String(50), nullable=True),
        sa.Column("parsed_path", sa.String(1000), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
    )
    op.create_index("idx_documents_status", "documents", ["status"])
    op.create_index("idx_documents_organization", "documents", ["organization_id"])
    op.create_unique_constraint("uq_documents_org_sha256", "documents", ["organization_id", "sha256"])

    # jobs
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("progress", sa.Float(), server_default="0", nullable=False),
        sa.Column("message", sa.String(500), server_default="", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
    )
    op.create_index("idx_jobs_document", "jobs", ["document_id"])

    # chunks
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("chapter_path", sa.String(500), server_default="", nullable=False),
        sa.Column("clause_no", sa.String(50), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("printed_page", sa.String(50), nullable=True),
        sa.Column("source_type", sa.String(50), server_default="text", nullable=False),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.create_index("idx_chunks_document", "chunks", ["document_id"])
    op.create_index("idx_chunks_clause", "chunks", ["clause_no"])

    # audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(100), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.create_index("idx_audit_logs_org", "audit_logs", ["organization_id"])
    op.create_index("idx_audit_logs_user", "audit_logs", ["user_id"])
    op.create_index("idx_audit_logs_created", "audit_logs", ["created_at"])

    # PostgreSQL 全文检索索引（仅在 PostgreSQL 下有效）
    # 创建 chunks_fts 的 tsvector 列用于全文检索
    # 注意：SQLite 的 FTS5 虚拟表在迁移时不创建，保留给 SQLite 兼容路径


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("chunks")
    op.drop_table("jobs")
    op.drop_table("documents")
    op.drop_table("organization_members")
    op.drop_table("users")
    op.drop_table("organizations")