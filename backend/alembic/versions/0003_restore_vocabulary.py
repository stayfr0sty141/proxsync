"""`interrupted` restores, and `restore_cancelled` in the audit trail

Two vocabulary widenings required by the restore flow (M6):

1. `restore_history.status` gains `interrupted`. A restore whose outcome the dashboard could
   not establish — it restarted after the agent was asked, or the agent stopped answering
   mid-restore — is not a failure. `qmrestore` and `pct restore` destroy and recreate the
   target, so recording "it did not happen" is the one answer that could get an operator to
   run it a second time over a restore that is still in progress.

2. `audit_events.action` gains `restore_cancelled`. Cancelling a restore is a distinct
   security-relevant act; folding it into `restore_confirmed` would make the trail claim the
   operator authorised the very thing they stopped.

Both changes rewrite a CHECK constraint, which SQLite can only do by rebuilding the table.
`copy_from` is spelled out because SQLAlchemy does not reflect CHECK constraints from SQLite:
a batch rebuild relying on reflection would silently drop every other constraint on the table.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import app.db.types
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

RESTORE_STATUS_BEFORE = (
    "status IN ('pending_confirmation', 'confirmed', 'running', 'success', 'failed', "
    "'cancelled', 'expired')"
)
RESTORE_STATUS_AFTER = (
    "status IN ('pending_confirmation', 'confirmed', 'running', 'success', 'failed', "
    "'cancelled', 'interrupted', 'expired')"
)

_AUDIT_ACTIONS_BEFORE = (
    "login_success",
    "login_failure",
    "logout",
    "token_refresh",
    "token_reuse_detected",
    "password_changed",
    "account_locked",
    "settings_changed",
    "user_created",
    "user_updated",
    "user_deleted",
    "backup_started",
    "backup_deleted",
    "restore_requested",
    "restore_confirmed",
    "retention_override",
)
_AUDIT_ACTIONS_AFTER = (
    *_AUDIT_ACTIONS_BEFORE[:-1],
    "restore_cancelled",
    _AUDIT_ACTIONS_BEFORE[-1],
)


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


AUDIT_ACTION_BEFORE = _in_list("action", _AUDIT_ACTIONS_BEFORE)
AUDIT_ACTION_AFTER = _in_list("action", _AUDIT_ACTIONS_AFTER)

STATUS_CONSTRAINT = "status_valid"
ACTION_CONSTRAINT = "action_valid"
"""The convention *tokens*, not the final identifiers.

`ck_%(table_name)s_%(constraint_name)s` interpolates `%(constraint_name)s`, so passing the
full `ck_restore_history_status_valid` here would look for
`ck_restore_history_ck_restore_history_status_valid` and find nothing.
"""

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _restore_history(status_check: str) -> sa.Table:
    """`restore_history` exactly as it exists at this revision.

    Spelled out rather than imported from the models: a migration is a snapshot, and one that
    read today's model would change meaning every time the model did.
    """
    return sa.Table(
        "restore_history",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "backup_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("target_vmid", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=8), nullable=False),
        sa.Column("restore_mode", sa.String(length=16), nullable=False),
        sa.Column("target_node", sa.String(length=64), nullable=False),
        sa.Column("target_storage", sa.String(length=64), nullable=False),
        sa.Column("overwrite_existing", sa.Boolean(), nullable=False),
        sa.Column("force_stop", sa.Boolean(), nullable=False),
        sa.Column("start_after_restore", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmation_token_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "confirmation_expires_at", app.db.types.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("confirmed_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("preflight", JSON_VARIANT, nullable=True),
        sa.Column("agent_task_id", sa.String(length=64), nullable=True),
        sa.Column(
            "requested_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("started_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("finished_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("restore_mode IN ('original_id', 'new_id')", name="restore_mode_valid"),
        sa.CheckConstraint("source IN ('local', 'gdrive')", name="source_valid"),
        sa.CheckConstraint(status_check, name=STATUS_CONSTRAINT),
        sa.CheckConstraint("target_type IN ('vm', 'lxc')", name="target_type_valid"),
        sa.ForeignKeyConstraint(["backup_id"], ["backup_history.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_restore_history_agent_task_id", "agent_task_id"),
        sa.Index("ix_restore_history_backup_id", "backup_id"),
        sa.Index("ix_restore_history_correlation_id", "correlation_id"),
        sa.Index("ix_restore_history_status_created", "status", "created_at"),
    )


def _audit_events(action_check: str) -> sa.Table:
    """`audit_events` exactly as it exists at this revision."""
    return sa.Table(
        "audit_events",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ts", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("username_snapshot", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("detail", JSON_VARIANT, nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(action_check, name=ACTION_CONSTRAINT),
        sa.CheckConstraint("result IN ('success', 'failure')", name="result_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_audit_events_action_ts", "action", "ts"),
        sa.Index("ix_audit_events_correlation_id", "correlation_id"),
        sa.Index("ix_audit_events_ts", "ts"),
    )


def _swap(
    table: str,
    build: Callable[[str], sa.Table],
    *,
    constraint: str,
    from_check: str,
    to_check: str,
) -> None:
    with op.batch_alter_table(table, schema=None, copy_from=build(from_check)) as batch_op:
        batch_op.drop_constraint(constraint, type_="check")
        batch_op.create_check_constraint(constraint, to_check)


def upgrade() -> None:
    _swap(
        "restore_history",
        _restore_history,
        constraint=STATUS_CONSTRAINT,
        from_check=RESTORE_STATUS_BEFORE,
        to_check=RESTORE_STATUS_AFTER,
    )
    _swap(
        "audit_events",
        _audit_events,
        constraint=ACTION_CONSTRAINT,
        from_check=AUDIT_ACTION_BEFORE,
        to_check=AUDIT_ACTION_AFTER,
    )


def downgrade() -> None:
    # An interrupted restore's defining property is that nobody knows what it did. `failed` is
    # the narrower vocabulary's closest value; the message preserves the part that matters.
    op.execute(
        "UPDATE restore_history SET status = 'failed', "
        "error_message = COALESCE(error_message, "
        "'Contact was lost while this restore was running; its outcome is unknown') "
        "WHERE status = 'interrupted'"
    )
    op.execute(
        "UPDATE audit_events SET action = 'restore_confirmed', result = 'failure' "
        "WHERE action = 'restore_cancelled'"
    )
    _swap(
        "audit_events",
        _audit_events,
        constraint=ACTION_CONSTRAINT,
        from_check=AUDIT_ACTION_AFTER,
        to_check=AUDIT_ACTION_BEFORE,
    )
    _swap(
        "restore_history",
        _restore_history,
        constraint=STATUS_CONSTRAINT,
        from_check=RESTORE_STATUS_AFTER,
        to_check=RESTORE_STATUS_BEFORE,
    )
