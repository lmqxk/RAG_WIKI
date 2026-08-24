"""sha256 全局唯一 → (organization_id, sha256) 复合唯一约束。

Revision ID: 002
Revises: 001
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # PostgreSQL: 先删除旧唯一约束，再创建复合唯一约束
        op.drop_constraint("uq_documents_sha256", "documents", type_="unique")
        op.create_unique_constraint(
            "uq_documents_org_sha256",
            "documents",
            ["organization_id", "sha256"],
        )
    else:
        # SQLite: 不支持 DROP CONSTRAINT，使用重建表策略
        with op.batch_alter_table("documents") as batch_op:
            batch_op.drop_constraint("uq_documents_sha256", type_="unique")
            batch_op.create_unique_constraint(
                "uq_documents_org_sha256",
                ["organization_id", "sha256"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_constraint("uq_documents_org_sha256", "documents", type_="unique")
        op.create_unique_constraint("uq_documents_sha256", "documents", ["sha256"])
    else:
        with op.batch_alter_table("documents") as batch_op:
            batch_op.drop_constraint("uq_documents_org_sha256", type_="unique")
            batch_op.create_unique_constraint("uq_documents_sha256", ["sha256"])