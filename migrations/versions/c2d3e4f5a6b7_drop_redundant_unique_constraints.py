"""drop redundant UNIQUE constraints that duplicate unique indexes (fixes `alembic check` on Postgres)

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6

Earlier migrations declared both a UNIQUE constraint and a unique index on the same column for
email_verification_tokens.token_hash, password_reset_tokens.token_hash and subscriptions.user_id.
The ORM models only declare the unique index, so `alembic check` reported drift on Postgres.
Uniqueness is still enforced by the surviving unique index; only the duplicate constraint goes.
SQLite never reported this drift and is left untouched.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

_TARGETS = [
    ("email_verification_tokens", ["token_hash"]),
    ("password_reset_tokens", ["token_hash"]),
    ("subscriptions", ["user_id"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    insp = sa.inspect(bind)
    for table, cols in _TARGETS:
        indexes = [i for i in insp.get_indexes(table) if i.get("unique") and list(i["column_names"]) == cols]
        if not indexes:  # never drop the only uniqueness guarantee
            continue
        for uc in insp.get_unique_constraints(table):
            if list(uc["column_names"]) == cols and uc.get("name"):
                op.drop_constraint(uc["name"], table, type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table, cols in _TARGETS:
        op.create_unique_constraint(f"{table}_{cols[0]}_key", table, cols)
