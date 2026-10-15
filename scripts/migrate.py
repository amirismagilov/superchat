#!/usr/bin/env python3
"""Create/validate the schema; reviewed additive 0001 -> 0002 upgrade only."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Column, MetaData, String, Table, inspect, select, text

from app.config import settings
from app.db import Base, IntegrationSetting, engine


REVISION = "0002_telegram_integration"
PREVIOUS_REVISION = "0001_initial"
PREVIOUS_FINGERPRINT = "aeb4968bcf77d102581f0af87370e319a518e221c950f6fb9176ef901441f890"
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
            if current.revision == PREVIOUS_REVISION and current.model_fingerprint == PREVIOUS_FINGERPRINT:
                original_tables = [table for table in Base.metadata.sorted_tables if table.name != IntegrationSetting.__tablename__]
                if fingerprint(original_tables) != PREVIOUS_FINGERPRINT:
                    raise RuntimeError("Original models have changed; refusing an unreviewed upgrade")
                validate_tables(connection, original_tables)
                if IntegrationSetting.__tablename__ in existing:
                    raise RuntimeError("Unversioned integration_settings table exists; inspect before upgrade")
                # Only add an empty settings table. Existing rows, account keys,
                # messages, outboxes, provider sessions and volumes are untouched.
                IntegrationSetting.__table__.create(connection)
                connection.execute(revision_table.update().values(revision=REVISION, model_fingerprint=model_hash))
                print(f"Upgraded {PREVIOUS_REVISION} -> {REVISION}; existing records preserved.")
            elif current.revision != REVISION or current.model_fingerprint != model_hash:
                raise RuntimeError("Unknown or changed schema revision; supply a reviewed upgrade migration")
            validate_tables(connection)
            print(f"Schema {REVISION} structure validated.")


if __name__ == "__main__":
    main()
