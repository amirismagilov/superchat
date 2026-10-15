import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.base import Event
from app.db import Account, Base, Conversation, Message, PendingReceipt, now
from app.services import ingest, reconcile_receipts


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = Account(tenant_id="tenant", owner_id="owner", channel="telegram", label="Work", encrypted_config="unused")
        session.add(account)
        session.flush()
        yield session, account
    engine.dispose()


def receipt(status):
    return Event("message-1", "123", "", direction="outbound", status=status, metadata={"status_only": True})


def test_reordered_failure_cannot_erase_delivery_evidence(store):
    session, account = store
    ingest(session, account, Event("message-1", "123", "Hello", direction="outbound", status="read"))
    for status in ("sent", "failed", "delivered"):
        ingest(session, account, receipt(status))
    assert session.scalar(select(Message)).status == "read"
    assert len(list(session.scalars(select(Message)))) == 1


def test_delivery_receipt_recovers_previous_failure(store):
    session, account = store
    ingest(session, account, Event("message-1", "123", "Hello", direction="outbound", status="sent"))
    ingest(session, account, receipt("failed"))
    message = session.scalar(select(Message))
    assert message.status == "failed"
    message.error = "transport_timeout"
    ingest(session, account, receipt("delivered"))
    assert message.status == "delivered"
    assert message.error is None


def test_late_send_acknowledgement_does_not_erase_failure(store):
    session, account = store
    ingest(session, account, Event("message-1", "123", "Hello", direction="outbound", status="failed"))
    ingest(session, account, receipt("sent"))
    assert session.scalar(select(Message)).status == "failed"


def test_known_alias_is_persisted_as_canonical_peer(store):
    session, account = store
    conversation = Conversation(account_id=account.id, peer="@alice", title="Alice")
    session.add(conversation)
    session.flush()
    original_id = conversation.id
    ingest(session, account, Event("one", "123", "First", metadata={"canonical_peer": "123", "aliases": ["@alice"]}))
    session.commit()
    ingest(session, account, Event("two", "123", "Second"))
    assert conversation.id == original_id
    assert conversation.peer == "123"
    assert len(list(session.scalars(select(Conversation)))) == 1
    assert [m.conversation_id for m in session.scalars(select(Message))] == [original_id, original_id]


def test_colliding_aliases_preserve_histories_and_surface_conflict(store):
    session, account = store
    ingest(session, account, Event("one", "@alice", "Old history"))
    ingest(session, account, Event("two", "123", "Other history"))
    canonical = session.scalar(select(Conversation).where(Conversation.peer == "123"))
    ingest(session, account, Event("three", "123", "New message", metadata={"aliases": ["@alice"]}))
    assert len(list(session.scalars(select(Conversation)))) == 2
    assert len(list(session.scalars(select(Message)))) == 3
    assert session.scalar(select(Message).where(Message.provider_id == "three")).conversation_id == canonical.id
    conflict = account.public["identity_conflicts"][0]
    assert conflict["canonical_peer"] == "123"
    assert len(conflict["conversation_ids"]) == 2


def test_unknown_status_does_not_create_empty_conversation(store):
    session, account = store
    ingest(session, account, receipt("read"))
    assert list(session.scalars(select(Conversation))) == []
    assert session.scalar(select(PendingReceipt)).status == "read"


def test_two_old_aliases_do_not_create_third_chat_on_next_event(store):
    session, account = store
    ingest(session, account, Event("one", "@alice", "Username history"))
    ingest(session, account, Event("two", "+15551234567", "Phone history"))
    ingest(session, account, Event("three", "123", "New message", metadata={"aliases": ["@alice", "+15551234567"]}))
    ingest(session, account, Event("four", "123", "Next message"))
    assert len(list(session.scalars(select(Conversation)))) == 2
    assert len(list(session.scalars(select(Message)))) == 4
    messages = list(session.scalars(select(Message).where(Message.provider_id.in_(["three", "four"]))))
    assert messages[0].conversation_id == messages[1].conversation_id


def test_replayed_outbound_event_updates_receipt_without_duplicate(store):
    session, account = store
    ingest(session, account, Event("message-1", "123", "Hello", direction="outbound", status="sent"))
    ingest(session, account, Event("message-1", "123", "Hello", direction="outbound", status="read"))
    assert len(list(session.scalars(select(Message)))) == 1
    assert session.scalar(select(Message)).status == "read"


def test_early_receipt_survives_commit_and_reconciles_after_send(store):
    session, account = store
    conversation = Conversation(account_id=account.id, peer="123", title="Alice")
    session.add(conversation)
    session.flush()
    message = Message(conversation_id=conversation.id, client_id="client", text="Hello", direction="outbound", status="sending")
    session.add(message)
    ingest(session, account, receipt("read"))
    session.commit()
    message.provider_id = "message-1"
    message.status = "sent"
    reconcile_receipts(session, account, message, "123")
    assert message.status == "read"
    assert list(session.scalars(select(PendingReceipt))) == []
    assert len(list(session.scalars(select(Message)))) == 1


def test_early_whatsapp_receipt_uses_client_id_within_its_conversation(store):
    session, account = store
    conversation = Conversation(account_id=account.id, peer="123", title="Alice")
    session.add(conversation)
    session.flush()
    message = Message(conversation_id=conversation.id, client_id="client", text="Hello", direction="outbound", status="sending")
    session.add(message)
    event = receipt("delivered")
    event.metadata["client_id"] = "client"
    ingest(session, account, event)
    assert message.status == "delivered"
    assert message.provider_id == "message-1"
    assert list(session.scalars(select(PendingReceipt))) == []


def test_receipts_cannot_attach_to_other_peer_or_account(store):
    session, account = store
    other_account = Account(tenant_id="other", owner_id="owner", channel="telegram", label="Other", encrypted_config="unused")
    session.add(other_account)
    session.flush()
    ingest(session, account, receipt("read"))
    ingest(session, other_account, Event("message-1", "123", "Other tenant", direction="outbound", status="sent"))
    ingest(session, account, Event("message-1", "456", "Other peer", direction="outbound", status="sent"))
    assert [m.status for m in session.scalars(select(Message))] == ["sent", "sent"]
    assert session.scalar(select(PendingReceipt)).status == "read"


def test_reordered_pending_receipts_keep_delivery_evidence(store):
    session, account = store
    for status in ("read", "failed", "sent"):
        ingest(session, account, receipt(status))
    session.commit()
    ingest(session, account, Event("message-1", "123", "Echo", direction="outbound", status="sent"))
    assert session.scalar(select(Message)).status == "read"
    assert list(session.scalars(select(PendingReceipt))) == []


def test_import_does_not_open_whatsapp_reply_window(store):
    session, account = store
    ingest(session, account, Event("old", "123", "Imported", timestamp=now().timestamp(), metadata={"historical": True}))
    conversation = session.scalar(select(Conversation))
    assert conversation.last_message_at is not None
    assert conversation.last_inbound_at is None
