"""Tenant-scoped inbox, discovery, read acknowledgements and response waiting."""
from datetime import timedelta

import pytest
from sqlalchemy import event as sql_event, select

from app.adapters.base import Event
from app.db import Account, Conversation, ConversationInbox, ConversationRead, Job, Message, MessageInbox, now
from app.security import Identity, seal, unseal
from app.services import ingest, inbox_page, record_message_sent
from test_core import app_client, auth, setup_conversation  # Reuse the authenticated API fixture.


def put_event(factory, account_id, provider_id, *, peer="15551234567", text="Message", **kwargs):
    with factory.begin() as db:
        ingest(db, db.get(Account, account_id), Event(provider_id, peer, text, **kwargs))
        message = db.scalar(select(Message).where(Message.provider_id == provider_id))
        return message.id if message else None


def test_directory_discovery_creates_no_bubbles_and_searches_cyrillic_names(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    put_event(factory, a, "discovery-only", peer="new-peer", text="", peer_name="ООО Ромашка",
              metadata={"conversation_only": True, "aliases": ["+79990000111"]})
    page = client.get("/api/recipients", headers=auth(), params={"q": "рОмАшКа"}).json()
    assert page["total"] == 1 and not page["has_more"]
    item = page["items"][0]
    assert item["title"] == "ООО Ромашка" and item["account_id"] == a
    assert item["preview"] is None and item["unread_count"] == 0 and item["waiting_since"] is None
    assert item["last_activity_at"] is None
    assert client.get(f"/api/conversations/{item['id']}/messages", headers=auth()).json() == []
    # Another discovery pass without aliases must not erase searchable phones.
    put_event(factory, a, "discovery-again", peer="new-peer", text="", peer_name="ООО Ромашка",
              metadata={"conversation_only": True})
    assert client.get("/api/recipients", headers=auth(), params={"q": "79990000111"}).json()["total"] == 1


def test_inbox_is_paginated_and_scoped_for_owner_tenant_account_and_channel(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory, channel="telegram")
    with factory.begin() as db:
        for index, (owner, tenant, channel) in enumerate([
            ("alice", "company", "whatsapp_web"), ("bob", "company", "telegram"), ("alice", "other", "telegram")]):
            account = Account(id=f"other-{index}", tenant_id=tenant, owner_id=owner, channel=channel,
                              label="Private account", state="connected", encrypted_config=seal({"secret": "not-public"}))
            db.add(account)
            db.flush()
            ingest(db, account, Event(f"seed-{index}", "same-name", "Private preview", peer_name="Same name"))
    first = client.get("/api/inbox?limit=1", headers=auth()).json()
    second = client.get("/api/inbox?limit=1&offset=1", headers=auth()).json()
    assert first["total"] == second["total"] == 2
    assert first["has_more"] and not second["has_more"]
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert client.get("/api/inbox?channel=telegram", headers=auth()).json()["total"] == 1
    assert client.get("/api/recipients", headers=auth(role="admin")).json()["total"] == 3
    assert client.get("/api/inbox?account_id=other-1", headers=auth()).status_code == 404
    assert client.get("/api/recipients?account_id=other-2", headers=auth(role="admin")).status_code == 404
    for path in ("/api/inbox", "/api/recipients"):
        body = client.get(path, headers=auth()).text
        assert "not-public" not in body and "encrypted_config" not in body
        assert client.get(path + "?limit=201", headers=auth()).status_code == 422
        assert client.get(path + "?offset=-1", headers=auth()).status_code == 422


def test_search_treats_percent_and_underscore_as_literal_characters(app_client):
    client, factory = app_client
    a, _ = setup_conversation(client, factory)
    put_event(factory, a, "one", peer="one", text="", peer_name="100%_Shop", metadata={"conversation_only": True})
    put_event(factory, a, "two", peer="two", text="", peer_name="Other Shop", metadata={"conversation_only": True})
    for q in ["%", "_", "100%_"]:
        page = client.get("/api/inbox", headers=auth(), params={"q": q}).json()
        assert page["total"] == 1 and page["items"][0]["title"] == "100%_Shop"


def test_patch_metadata_survives_provider_enrichment_and_preserves_authorization(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    update = {"title": "Мой клиент", "stage": "in_progress", "favorite": True}
    result = client.patch(f"/api/conversations/{c}", headers=auth(), json=update)
    assert result.status_code == 200 and all(result.json()[key] == value for key, value in update.items())
    put_event(factory, a, "name-change", peer_name="Provider name", metadata={"conversation_only": True})
    assert client.get("/api/inbox?q=КЛИЕНТ", headers=auth()).json()["items"][0]["title"] == "Мой клиент"
    for attacker in [auth("bob"), auth("alice", "other", "admin")]:
        assert client.patch(f"/api/conversations/{c}", headers=attacker, json=update).status_code == 404
    for invalid in [{}, {"stage": "unknown"}, {"favorite": "false"}, {"title": "  "}, {"title": None}, {"peer": "redirect"}]:
        assert client.patch(f"/api/conversations/{c}", headers=auth(), json=invalid).status_code == 422


def test_read_acknowledges_only_visible_inbound_and_is_monotonic_per_user(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    first = put_event(factory, a, "first")
    second = put_event(factory, a, "arrived-after-view")
    historical = put_event(factory, a, "backfill", metadata={"historical": True})
    outgoing = put_event(factory, a, "sent", direction="outbound", status="sent")
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["unread_count"] == 2
    response = client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": first})
    assert response.json() == {"conversation_id": c, "last_read_message_id": first, "unread_count": 1}
    assert client.get("/api/inbox", headers=auth("admin", role="admin")).json()["items"][0]["unread_count"] == 2
    assert client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": outgoing}).status_code == 422
    # Backfill can allocate a newer DB id for an old message after the browser
    # loaded its live slice; it must not hide the still unseen second message.
    assert historical > second
    assert client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": historical}).status_code == 422
    visible = client.get(f"/api/conversations/{c}/messages", headers=auth()).json()
    assert {m["id"] for m in visible if m["read_eligible"]} == {first, second}
    assert next(m for m in visible if m["id"] == historical)["historical"] is True
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["unread_count"] == 1
    assert client.post(f"/api/conversations/{c}/read", headers=auth("bob"), json={"last_message_id": first}).status_code == 404
    assert client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": 999999}).status_code == 422
    client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": second})
    assert client.post(f"/api/conversations/{c}/read", headers=auth(), json={"last_message_id": first}).json()["last_read_message_id"] == second
    with factory() as db:
        assert db.get(ConversationRead, (c, "alice")).last_message_id == second
        assert db.get(MessageInbox, historical).historical is True


def test_waiting_ignores_queued_failed_and_historical_inbound_and_clears_only_on_send(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    instant = now() - timedelta(minutes=5)
    put_event(factory, a, "old-history", timestamp=(instant - timedelta(days=1)).timestamp(), metadata={"historical": True})
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["waiting_since"] is None
    put_event(factory, a, "live-question", timestamp=instant.timestamp())
    queued = put_event(factory, a, "queued", direction="outbound", status="queued", timestamp=(instant - timedelta(minutes=1)).timestamp())
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["waiting_since"] is not None
    with factory.begin() as db:
        db.get(Message, queued).status = "failed"
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["waiting_since"] is not None
    with factory.begin() as db:
        message = db.get(Message, queued)
        message.status = "sent"
        record_message_sent(db, message)  # Actual acceptance is after the incoming question.
    assert client.get("/api/inbox", headers=auth()).json()["items"][0]["waiting_since"] is None
    with factory() as db:
        accepted = db.get(MessageInbox, queued).accepted_at
    put_event(factory, a, "queued", direction="outbound", status="read", timestamp=(instant - timedelta(minutes=1)).timestamp())
    with factory() as db:
        assert db.get(MessageInbox, queued).accepted_at == accepted


def test_inbox_uses_bounded_query_count_not_per_conversation_queries(app_client):
    client, factory = app_client
    a, _ = setup_conversation(client, factory)
    for index in range(30):
        put_event(factory, a, str(index), peer=str(index))
    calls = []
    engine = factory.kw["bind"]
    def observed(*args):
        calls.append(args[2])
    sql_event.listen(engine, "before_cursor_execute", observed)
    try:
        with factory() as db:
            result = inbox_page(db, Identity("alice", "company", "agent"), limit=20)
        assert len(result["items"]) == 20 and result["total"] == 31
        assert len(calls) == 2  # Count plus scoped paginated projection.
    finally:
        sql_event.remove(engine, "before_cursor_execute", observed)


def test_history_job_validation_is_scoped_and_strict(app_client):
    client, factory = app_client
    a, _ = setup_conversation(client, factory, channel="telegram")
    for payload in [{}, {"history_days": 3650}]:
        response = client.post(f"/api/accounts/{a}/actions", headers=auth(), json={"action": "history", "payload": payload})
        assert response.status_code == 202
        with factory() as db:
            assert unseal(db.get(Job, response.json()["id"]).encrypted_payload) == payload
    for invalid in [{"history_days": True}, {"history_days": 0}, {"history_days": "30"}, {"history_days": 3651}, {"peer": "arbitrary"}]:
        assert client.post(f"/api/accounts/{a}/actions", headers=auth(), json={"action": "history", "payload": invalid}).status_code == 422
    assert client.post(f"/api/accounts/{a}/actions", headers=auth("bob"), json={"action": "history"}).status_code == 404
    with factory.begin() as db:
        db.get(Account, a).state = "disconnected"
    assert client.post(f"/api/accounts/{a}/actions", headers=auth(), json={"action": "history"}).status_code == 409
