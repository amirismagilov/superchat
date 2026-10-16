"""Exercise the reviewed additive upgrade against actual persisted records."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db import Account, Base, Conversation, ConversationInbox, IntegrationSetting, Message, MessageInbox
from app.security import seal, unseal
from scripts import migrate


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.sqlite"))
    monkeypatch.setattr(migrate, "engine", engine)
    monkeypatch.setattr(migrate.settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(migrate.settings, "jwt_secret", "test-only-" * 8)
    monkeypatch.setattr(migrate.settings, "environment", "development")
    monkeypatch.setattr(migrate.settings, "local_test", False)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("revision,model_hash", [
    (migrate.INITIAL_REVISION, migrate.INITIAL_FINGERPRINT),
    (migrate.PREVIOUS_REVISION, migrate.PREVIOUS_FINGERPRINT),
])
def test_additive_upgrade_preserves_existing_sessions_history_and_outbox(migration_db, revision, model_hash):
    added = set(migrate.INBOX_TABLES)
    if revision == migrate.INITIAL_REVISION:
        added.add(IntegrationSetting.__tablename__)
    original_tables = [table for table in Base.metadata.sorted_tables if table.name not in added]
    assert migrate.fingerprint(original_tables) == model_hash
    Base.metadata.create_all(migration_db, tables=original_tables)
    migrate.revision_table.create(migration_db)
    with migration_db.begin() as connection:
        connection.execute(migrate.revision_table.insert().values(
            revision=revision, model_fingerprint=model_hash))
    secret = seal({"session": "previous-session", "api_id": 12345, "api_hash": "a" * 32})
    with Session(migration_db) as db:
        account = Account(id="account", tenant_id="tenant", owner_id="manager", channel="telegram",
                          label="Preserved account", state="connected", encrypted_config=secret)
        db.add(account)
        db.add(Conversation(id="conversation", account_id="account", peer="123", title="ООО Ромашка"))
        db.flush()
        db.add(Message(conversation_id="conversation", client_id="outbox-key", text="Preserved message",
                       status="queued", direction="outbound"))
        db.commit()
    migrate.main()
    migrate.main()  # Repeat deployment must only validate the known revision.
    with Session(migration_db) as db:
        assert db.get(Account, "account").encrypted_config == secret
        assert unseal(db.get(Account, "account").encrypted_config)["session"] == "previous-session"
        message = db.scalar(select(Message))
        assert (message.text, message.status, message.client_id) == ("Preserved message", "queued", "outbox-key")
        assert list(db.scalars(select(IntegrationSetting))) == []
        metadata = db.get(ConversationInbox, "conversation")
        assert metadata.title_custom is True
        assert metadata.search_text == "ооо ромашка\n123"
        assert metadata.favorite is False and metadata.stage == "new"
        assert list(db.scalars(select(MessageInbox))) == []
        # Old code can read/write its unchanged tables after rolling back the
        # application image; no data restore/schema downgrade is necessary.
        message.text = "Written by old application"
        db.commit()
    with migration_db.connect() as connection:
        revision = connection.execute(select(migrate.revision_table)).one()
        assert revision.revision == migrate.REVISION
        assert revision.model_fingerprint == migrate.fingerprint()


def test_new_database_initializes_current_revision_idempotently(migration_db):
    migrate.main()
    migrate.main()
    assert IntegrationSetting.__tablename__ in inspect(migration_db).get_table_names()


def test_unknown_old_schema_fingerprint_is_rejected_without_changes(migration_db):
    migrate.revision_table.create(migration_db)
    with migration_db.begin() as connection:
        connection.execute(migrate.revision_table.insert().values(
            revision=migrate.PREVIOUS_REVISION, model_fingerprint="unknown"))
    with pytest.raises(RuntimeError, match="Unknown or changed"):
        migrate.main()
    assert IntegrationSetting.__tablename__ not in inspect(migration_db).get_table_names()


def test_unversioned_inbox_table_is_not_overwritten(migration_db):
    original = [table for table in Base.metadata.sorted_tables if table.name not in migrate.INBOX_TABLES]
    Base.metadata.create_all(migration_db, tables=original)
    migrate.revision_table.create(migration_db)
    ConversationInbox.__table__.create(migration_db)
    with migration_db.begin() as connection:
        connection.execute(migrate.revision_table.insert().values(
            revision=migrate.PREVIOUS_REVISION, model_fingerprint=migrate.PREVIOUS_FINGERPRINT))
    with pytest.raises(RuntimeError, match="Unversioned additive"):
        migrate.main()
