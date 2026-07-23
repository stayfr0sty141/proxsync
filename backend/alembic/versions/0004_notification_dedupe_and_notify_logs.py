"""Outbox de-duplication key, and `notify` in the log category vocabulary

Two changes required by notifications and log persistence (M7):

1. `notifications.dedupe_key` records *which occurrence* a message is about, so the outbox can
   tell a repeated report of one event from a genuinely new one. Without it, suppression would
   have to guess from the payload, and a Telegram outage followed by a retry storm would be
   indistinguishable from ten separate failures.

2. `logs.category` gains `notify`. Delivery lines were previously homeless: folding "the alert
   was sent" into `system` means the one question an operator asks after an incident — *was I
   actually told?* — cannot be answered with a category filter.

Adding a column and an index needs no table rebuild on either engine. The CHECK constraint
does, and SQLAlchemy does not reflect CHECK constraints from SQLite, so `logs` is spelled out
in full: a batch rebuild relying on reflection would silently drop every other constraint.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import app.db.types
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

_CATEGORIES_BEFORE = (
    "api",
    "backup",
    "restore",
    "upload",
    "retention",
    "scheduler",
    "auth",
    "agent",
    "system",
)
_CATEGORIES_AFTER = (*_CATEGORIES_BEFORE[:-1], "notify", _CATEGORIES_BEFORE[-1])

CATEGORY_CONSTRAINT = "category_valid"
"""The convention *token*, not the final identifier — `ck_%(table_name)s_%(constraint_name)s`
interpolates this, so the full `ck_logs_category_valid` would resolve to a name that does not
exist."""

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


CATEGORY_BEFORE = _in_list("category", _CATEGORIES_BEFORE)
CATEGORY_AFTER = _in_list("category", _CATEGORIES_AFTER)


def _logs(category_check: str) -> sa.Table:
    """`logs` exactly as it exists at this revision.

    Spelled out rather than imported from the models: a migration is a snapshot, and one that
    read today's model would change meaning every time the model did.
    """
    return sa.Table(
        "logs",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ts", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", JSON_VARIANT, nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("backup_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column(
            "restore_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column(
            "sync_task_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.CheckConstraint(category_check, name=CATEGORY_CONSTRAINT),
        sa.CheckConstraint(
            "level IN ('debug', 'info', 'warning', 'error', 'critical')", name="level_valid"
        ),
        sa.ForeignKeyConstraint(["backup_id"], ["backup_history.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["restore_id"], ["restore_history.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sync_task_id"], ["sync_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_logs_category_ts", "category", "ts"),
        sa.Index("ix_logs_correlation_id", "correlation_id"),
        sa.Index("ix_logs_level_ts", "level", "ts"),
        sa.Index("ix_logs_ts", "ts"),
    )


def _swap_category_check(*, from_check: str, to_check: str) -> None:
    with op.batch_alter_table("logs", schema=None, copy_from=_logs(from_check)) as batch_op:
        batch_op.drop_constraint(CATEGORY_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(CATEGORY_CONSTRAINT, to_check)


def upgrade() -> None:
    op.add_column("notifications", sa.Column("dedupe_key", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_notifications_dedupe",
        "notifications",
        ["channel", "event_type", "dedupe_key", "created_at"],
        unique=False,
    )
    _swap_category_check(from_check=CATEGORY_BEFORE, to_check=CATEGORY_AFTER)


def downgrade() -> None:
    # Rows written under the wider vocabulary have to be reclassified before the narrower CHECK
    # is restored, or the rebuild fails on its own data. `system` is where these lines lived
    # before this revision existed.
    op.execute("UPDATE logs SET category = 'system' WHERE category = 'notify'")
    _swap_category_check(from_check=CATEGORY_AFTER, to_check=CATEGORY_BEFORE)
    op.drop_index("ix_notifications_dedupe", table_name="notifications")
    op.drop_column("notifications", "dedupe_key")
