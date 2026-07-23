"""Run plan/options, and `cancelled` as a backup outcome

Two changes, both required by the backup engine (M3):

1. `backup_runs.targets` and `backup_runs.options` — the resolved plan and the settings a run
   will use, frozen when the run is *requested*. Without them a queued run would read its
   options at execution time, so editing a schedule would retroactively change a run already
   in the queue, and a guest removed from Proxmox would vanish from the record of what the
   run intended to do.

2. `cancelled` added to the `backup_history.status` vocabulary. A backup an operator stopped
   is not a failure, and folding the two together would make the dashboard's failed-backup
   count include deliberate cancellations.

The second change rewrites a CHECK constraint, which SQLite can only do by rebuilding the
table. `copy_from` is given explicitly because SQLAlchemy does not reflect CHECK constraints
from SQLite — a batch rebuild that relied on reflection would silently drop every other
constraint on this table.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import app.db.types
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

STATUS_BEFORE = "status IN ('running', 'success', 'failed', 'interrupted', 'deleted')"
STATUS_AFTER = "status IN ('running', 'success', 'failed', 'cancelled', 'interrupted', 'deleted')"

STATUS_CONSTRAINT = "status_valid"
"""The convention *token*, not the final identifier.

`ck_%(table_name)s_%(constraint_name)s` contains `%(constraint_name)s`, which means
SQLAlchemy applies it to explicitly named check constraints as well as anonymous ones.
Passing the full `ck_backup_history_status_valid` here would therefore look for
`ck_backup_history_ck_backup_history_status_valid` and find nothing.
"""

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _backup_history(status_check: str) -> sa.Table:
    """`backup_history` exactly as it exists at this revision.

    Spelled out rather than imported from the models: a migration is a snapshot, and one that
    read today's model would change meaning every time the model did.
    """
    return sa.Table(
        "backup_history",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("guest_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("guest_type", sa.String(length=8), nullable=False),
        sa.Column("guest_name", sa.String(length=128), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column("storage", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("local_path", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("compression", sa.String(length=16), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("agent_task_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("finished_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warnings", JSON_VARIANT, nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.Column("upload_status", sa.String(length=16), nullable=False),
        sa.Column("uploaded_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("remote_path", sa.String(length=512), nullable=True),
        sa.Column("remote_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("remote_checksum", sa.String(length=64), nullable=True),
        sa.Column("retention_locked", sa.Boolean(), nullable=False),
        sa.Column("local_deleted_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("remote_deleted_at", app.db.types.UTCDateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "compression IN ('zstd', 'gzip', 'lzo', 'none')",
            name="compression_valid",
        ),
        sa.CheckConstraint("guest_type IN ('vm', 'lxc')", name="guest_type_valid"),
        sa.CheckConstraint("mode IN ('snapshot', 'suspend', 'stop')", name="mode_valid"),
        sa.CheckConstraint(status_check, name=STATUS_CONSTRAINT),
        sa.CheckConstraint(
            "upload_status IN ('not_required', 'pending', 'uploading', 'uploaded', 'failed')",
            name="upload_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"],
            ["guests.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backup_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage", "filename", name="uq_backup_history_storage_filename"),
        sa.Index("ix_backup_history_agent_task_id", "agent_task_id"),
        sa.Index("ix_backup_history_correlation_id", "correlation_id"),
        sa.Index("ix_backup_history_guest_started", "vmid", "guest_type", "started_at"),
        sa.Index("ix_backup_history_run_id", "run_id"),
        sa.Index("ix_backup_history_status", "status"),
        sa.Index("ix_backup_history_upload_status", "upload_status"),
    )


def _swap_status_constraint(*, from_check: str, to_check: str) -> None:
    with op.batch_alter_table(
        "backup_history", schema=None, copy_from=_backup_history(from_check)
    ) as batch_op:
        batch_op.drop_constraint(STATUS_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(STATUS_CONSTRAINT, to_check)


def upgrade() -> None:
    with op.batch_alter_table("backup_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("targets", JSON_VARIANT, nullable=True))
        batch_op.add_column(sa.Column("options", JSON_VARIANT, nullable=True))

    _swap_status_constraint(from_check=STATUS_BEFORE, to_check=STATUS_AFTER)


def downgrade() -> None:
    # Anything already recorded as cancelled would violate the narrower constraint. It is
    # closer to the truth than 'success' and preserves the fact that the backup does not
    # exist, which is what any consumer of this row actually needs to know.
    op.execute(
        "UPDATE backup_history SET status = 'failed', "
        "error_message = COALESCE(error_message, 'Cancelled by an operator') "
        "WHERE status = 'cancelled'"
    )
    _swap_status_constraint(from_check=STATUS_AFTER, to_check=STATUS_BEFORE)

    with op.batch_alter_table("backup_runs", schema=None) as batch_op:
        batch_op.drop_column("options")
        batch_op.drop_column("targets")
