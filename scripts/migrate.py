#!/usr/bin/env python3
"""Create/validate the schema; reviewed additive 0001 -> 0002 -> 0003 upgrades."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Column, MetaData, String, Table, inspect, select, text

from app.config import settings
from app.db import Base, Conversation, ConversationInbox, IntegrationSetting, engine


REVISION = "0003_inbox"
PREVIOUS_REVISION = "0002_telegram_integration"
PREVIOUS_FINGERPRINT = "b697e7c84123936ccf4f449cda1de314fddc38e6df7f9325f6438b24710ef786"
INITIAL_REVISION = "0001_initial"
INITIAL_FINGERPRINT = "aeb4968bcf77d102581f0af87370e319a518e221c950f6fb9176ef901441f890"
INBOX_TABLES = frozenset({"conversation_inbox", "conversation_reads", "message_inbox"})
revision_table = Table(
    "omnicrm_schema_revision", MetaData(),
    Column("revision", String(64), primary_key=True),
    Column("model_fingerprint", String(64), nullable=False),
)


def fingerprint(tables=None):
    structure = []
    for table in Base.metadata.sorted_tables if tables is None else tables:
        structure.append({
            "name": table.name,
            "columns": [(c.name, str(c.type), c.nullable, c.primary_key) for c in table.columns],
            "constraints": sorted(
                (type(c).__name__, tuple(col.name for col in c.columns))
                for c in table.constraints
            ),
            "indexes": sorted(
                (idx.name, tuple(col.name for col in idx.columns), idx.unique)
                for idx in table.indexes
            ),
        })
    return hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()


def validate_tables(connection, tables=None):
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables if tables is None else tables:
        if not inspector.has_table(table.name):
            raise RuntimeError("Schema is incomplete; restore or apply a reviewed migration")
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        if actual != set(table.columns.keys()):
            raise RuntimeError("Schema columns differ; an explicit upgrade migration is required")
        actual_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        if not {idx.name for idx in table.indexes}.issubset(actual_indexes):
            raise RuntimeError("Schema indexes differ; an explicit upgrade migration is required")


def main():
    settings.validate()
    model_hash = fingerprint()
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Serialize initializers, without relying on the worker's locks.
            connection.execute(text("SELECT pg_advisory_xact_lock(532493001)"))
        existing = set(inspect(connection).get_table_names())
        if revision_table.name not in existing:
            if existing:
                raise RuntimeError("Refusing unversioned non-empty database; initial schema creation requires an empty database")
            Base.metadata.create_all(connection)
            revision_table.create(connection)
            connection.execute(revision_table.insert().values(
                revision=REVISION, model_fingerprint=model_hash,
            ))
            validate_tables(connection)
            print(f"Schema {REVISION} created in an empty database.")
        else:
            revisions = connection.execute(select(revision_table)).all()
            if len(revisions) != 1:
                raise RuntimeError("Unknown or changed schema revision; supply a reviewed upgrade migration")
            current = revisions[0]
            previous = {PREVIOUS_REVISION: PREVIOUS_FINGERPRINT, INITIAL_REVISION: INITIAL_FINGERPRINT}
            if current.revision in previous and current.model_fingerprint == previous[current.revision]:
                added = set(INBOX_TABLES)
                if current.revision == INITIAL_REVISION:
                    added.add(IntegrationSetting.__tablename__)
                original_tables = [table for table in Base.metadata.sorted_tables if table.name not in added]
                if fingerprint(original_tables) != previous[current.revision]:
                    raise RuntimeError("Original models have changed; refusing an unreviewed upgrade")
                validate_tables(connection, original_tables)
                if added.intersection(existing):
                    raise RuntimeError("Unversioned additive table exists; inspect before upgrade")
                Base.metadata.create_all(connection, tables=[table for table in Base.metadata.sorted_tables if table.name in added])
                # Only populate the new metadata table. Existing account keys,
                # messages, outboxes and provider session bytes are untouched.
                # Old messages deliberately get no live-inbound marker: importing
                # this feature must not suddenly turn an old archive unread.
                cursor = ""
                while True:
                    rows = connection.execute(select(Conversation.id, Conversation.title, Conversation.peer)
                                              .where(Conversation.id > cursor).order_by(Conversation.id).limit(500)).all()
                    if not rows:
                        break
                    connection.execute(ConversationInbox.__table__.insert(), [{
                        "conversation_id": row.id, "stage": "new", "favorite": False,
                        "title_custom": bool(row.title and row.title != row.peer),
                        "search_text": "\n".join([row.title, row.peer]).casefold()[:8192],
                        "peer_aliases": [],
                    } for row in rows])
                    cursor = rows[-1].id
                connection.execute(revision_table.update().values(revision=REVISION, model_fingerprint=model_hash))
                print(f"Upgraded {current.revision} -> {REVISION}; existing records preserved.")
            elif current.revision != REVISION or current.model_fingerprint != model_hash:
                raise RuntimeError("Unknown or changed schema revision; supply a reviewed upgrade migration")
            validate_tables(connection)
            print(f"Schema {REVISION} structure validated.")


if __name__ == "__main__":
    main()
