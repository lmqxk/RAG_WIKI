"""Alembic 迁移模板。"""

revision: str = "${up_revision}"
down_revision: str | None = "${down_revision}"
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}