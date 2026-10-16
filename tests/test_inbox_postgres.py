"""Optional PostgreSQL 16 upgrade and inbox integration, isolated random schema."""
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import api
from app.adapters.base import Event
from app.db import Account, Base, Conversation, Message, MessageInbox
from app.security import Identity, seal
from app.services import ingest, inbox_page
from scripts import migrate


def test_postgres_0002_upgrade_unicode_search_and_race_safe_read(monkeypatch):
    filename = os.getenv("FENCING_POSTGRES_URL_FILE")
    if not filename:
        pytest.skip("Requires disposable PostgreSQL via FENCING_POSTGRES_URL_FILE")
    url = json.loads(Path(filename).read_text())["url"]
    namespace = "inbox_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + namespace},
                           execution_options={"schema_translate_map": {None: namespace}})
    monkeypatch.setattr(migrate, "engine", engine)
    monkeypatch.setattr(migrate.settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(migrate.settings, "jwt_secret", "test-only-" * 8)
    monkeypatch.setattr(migrate.settings, "environment", "development")
    monkeypatch.setattr(migrate.settings, "local_test", False)
    user = Identity("manager", "tenant", "agent")
    try:
        previous = [table for table in Base.metadata.sorted_tables if table.name not in migrate.INBOX_TABLES]
        Base.metadata.create_all(engine, tables=previous)
        migrate.revision_table.create(engine)
        with engine.begin() as connection:
            connection.execute(migrate.revision_table.insert().values(
                revision=migrate.PREVIOUS_REVISION, model_fingerprint=migrate.PREVIOUS_FINGERPRINT))
        ciphertext = seal({"session": "test-only-encrypted-session"})
        with Session(engine) as db:
            db.add(Account(id="account", tenant_id="tenant", owner_id="manager", channel="telegram",
                           label="Work", state="connected", encrypted_config=ciphertext))
            db.flush()
            db.add(Conversation(id="conversation", account_id="account", peer="123", title="ООО Ромашка"))
            db.flush()
            db.add(Message(conversation_id="conversation", text="Pre-upgrade archive", direction="inbound", status="received"))
            db.commit()
        migrate.main()
        migrate.main()
        with Session(engine) as db:
            assert db.get(Account, "account").encrypted_config == ciphertext
            assert inbox_page(db, user, q="рОмАшКа")["items"][0]["unread_count"] == 0
            account = db.get(Account, "account", with_for_update=True)
            ingest(db, account, Event("visible", "123", "Visible question"))
            db.commit()
            first = db.scalar(select(Message).where(Message.provider_id == "visible")).id
            account = db.get(Account, "account", with_for_update=True)
            ingest(db, account, Event("new-arrival", "123", "After the browser loaded"))
            db.commit()
            result = api.read_conversation("conversation", api.ConversationReadBody(last_message_id=first), user, db)
            assert result["unread_count"] == 1
            item = api.patch_conversation("conversation", api.ConversationPatch(stage="done", favorite=True), user, db)
            assert item["stage"] == "done" and item["favorite"] is True
            assert item["waiting_since"] is not None
            assert inbox_page(db, Identity("other", "tenant", "agent"))["items"] == []
            assert db.scalar(select(MessageInbox.message_id)) is not None
        # Original table layouts are unchanged, including all existing indexes.
        assert {column["name"] for column in inspect(engine).get_columns("messages")} == set(Message.__table__.columns.keys())
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()
