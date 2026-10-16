"""Live incoming messages must not wait for, or fall behind, stored history.

The fake implements WAHA 2026.9.2 timestamp filters before ordering/pagination;
it never returns messages outside the adapter's requested window. Both the
per-chat endpoint and NOWEB's persistent-store ``all`` sentinel are supported.
No real provider, credentials, account login, or network is involved.
"""
import copy
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters import whatsapp_web
from app.adapters.whatsapp_web import WhatsAppWebAdapter
from app.db import Account, Base, Conversation, Message
from app.services import ingest


SESSION = "omni-" + "1" * 32
PHONE = "79990000000@c.us"
LID = "123456789@lid"
GROUP = "12345-67890@g.us"


def stored_message(identifier, timestamp, *, peer=PHONE, outbound=False):
    # Exact NOWEB getFromToParticipant contract: `from` is the remote chat for
    # both directions; a DM need not contain `to`. Groups also expose `to`.
    value = {
        "id": f"{'true' if outbound else 'false'}_{peer}_{identifier}",
        "timestamp": timestamp,
        "from": peer,
        "fromMe": outbound,
        "body": identifier,
        "hasMedia": False,
        "ack": 2 if outbound else 0,
    }
    if peer.endswith("@g.us"):
        value["to"] = peer
        value["participant"] = PHONE
    return value


class StoredHistory:
    def __init__(self, peers=(PHONE,)):
        self.peers = list(peers)
        self.messages = []
        self.queries = []
        self.lid_pn = PHONE
        self.lid_lookups = 0

    @staticmethod
    def page(rows, params, *, field, default_limit):
        key = params.get("sortBy", field)
        if key == "messageTimestamp":
            key = "timestamp"
        rows = sorted(rows, key=lambda row: row.get(key, 0),
                      reverse=params.get("sortOrder", "asc") == "desc")
        offset = int(params.get("offset", "0"))
        limit = int(params.get("limit", str(default_limit)))
        assert 0 <= offset and 1 <= limit <= 1000
        return rows[offset:offset + limit]

    def request(self, request):
        assert request.method == "GET", "Receiving must not mutate provider auth/session"
        path, params = unquote(request.url.path), request.url.params
        prefix = f"/api/{SESSION}"
        if path == f"{prefix}/chats":
            rows = [{"id": peer, "name": "Customer", "conversationTimestamp": 0}
                    for peer in self.peers]
            return httpx.Response(200, json=self.page(rows, params, field="id", default_limit=20))
        if path == f"{prefix}/lids/{LID}":
            self.lid_lookups += 1
            return httpx.Response(200, json={"lid": LID, "pn": self.lid_pn})
        if path.startswith(f"{prefix}/chats/") and path.endswith("/messages"):
            peer = path[len(f"{prefix}/chats/"):-len("/messages")]
            all_chats = peer in {"all", "all@c.us", "all@s.whatsapp.net"}
            # The global message store may expose a chat before the separate
            # chat index catches up. Progressive discovery can then backfill it.
            known_peers = set(self.peers) | {message["from"] for message in self.messages}
            assert all_chats or peer in known_peers, "Unexpected history target"
            rows = [message for message in self.messages
                    if all_chats or message["from"] == peer]
            floor = float(params.get("filter.timestamp.gte", "-inf"))
            ceiling = float(params.get("filter.timestamp.lte", "inf"))
            rows = [message for message in rows if floor <= message["timestamp"] <= ceiling]
            if "filter.fromMe" in params:
                assert params["filter.fromMe"] in {"true", "false"}
                outgoing = params["filter.fromMe"] == "true"
                rows = [message for message in rows if message["fromMe"] == outgoing]
            if "filter.ack" in params:
                rows = [message for message in rows if message["ack"] == int(params["filter.ack"])]
            self.queries.append((peer, floor, ceiling, int(params.get("offset", "0"))))
            return httpx.Response(200, json=self.page(rows, params, field="timestamp", default_limit=10))
        pytest.fail(f"Unexpected WAHA read endpoint: {path}")

    def adapter(self, config=None):
        adapter = WhatsAppWebAdapter(config if config is not None else {"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha.invalid",
                                        transport=httpx.MockTransport(self.request))
        return adapter


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100)
    monkeypatch.setenv("ENABLE_EXPERIMENTAL", "true")
    monkeypatch.setattr(whatsapp_web, "time", SimpleNamespace(time=lambda: value.now))
    return value


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = Account(tenant_id="company", owner_id="manager", channel="whatsapp_web",
                          label="Work", state="connected", encrypted_config="unused")
        db.add(account)
        db.commit()
        yield db, account
    engine.dispose()


def persist(database, events):
    db, account = database
    for event in events:
        ingest(db, account, event)
    db.commit()


@pytest.mark.asyncio
async def test_delayed_store_message_before_previous_empty_watermark_is_received(clock, database):
    store = StoredHistory()
    adapter = store.adapter()
    try:
        discovered = await adapter.poll()  # Empty history advances to t=99.
        assert len(discovered) == 1 and discovered[0].metadata["conversation_only"]
        persist(database, discovered)
        assert list(database[0].scalars(select(Message))) == []
        assert database[0].scalar(select(Conversation)).peer == PHONE
        saved = copy.deepcopy(adapter.config)
    finally:
        await adapter.close()

    # The worker restarts. WAHA stores a slightly delayed incoming message with
    # its original provider timestamp, behind our last empty poll's wall clock.
    late = stored_message("late-reply", 98)
    store.messages.append(late)
    clock.now = 103
    restored = store.adapter(saved)
    try:
        events = await restored.poll()
        assert any(event.provider_id == late["id"] and event.direction == "inbound"
                   and event.peer == PHONE for event in events)
        persist(database, events)
        clock.now = 106
        persist(database, await restored.poll())
    finally:
        await restored.close()
    db, _ = database
    received = list(db.scalars(select(Message).where(Message.provider_id == late["id"])))
    assert len(received) == 1  # Overlap/replay must not produce duplicate bubbles.


@pytest.mark.asyncio
async def test_new_reply_arrives_while_old_history_is_still_being_imported(clock, database):
    clock.now = 10_000
    store = StoredHistory()
    # Several history pages ensure the old fixed ceiling remains open when the
    # new message arrives. A live tail may independently replay recent history.
    old = [stored_message(f"old-{index}", 9000 + index)
           for index in range(WhatsAppWebAdapter.PAGE_SIZE * 4 + 5)]
    store.messages.extend(old)
    adapter = store.adapter()
    try:
        persist(database, await adapter.poll())
        live = stored_message("new-live-reply", 10_001)
        store.messages.append(live)
        clock.now = 10_003
        events = await adapter.poll()
        assert any(event.provider_id == live["id"] and event.direction == "inbound"
                   for event in events), "New replies must not wait for the old history ceiling to drain"
        persist(database, events)
        for _ in range(len(old) // WhatsAppWebAdapter.PAGE_SIZE + 4):
            clock.now += 3
            persist(database, await adapter.poll())
    finally:
        await adapter.close()
    db, _ = database
    messages = list(db.scalars(select(Message)))
    assert {message.provider_id for message in messages} == {item["id"] for item in [*old, live]}
    assert len(messages) == len(old) + 1


@pytest.mark.asyncio
async def test_live_records_keep_remote_peer_direction_and_lid_mapping(clock, database):
    # Message writes may become visible before the chat index. All first-poll
    # records must therefore come from the global endpoint, not /chats pages.
    store = StoredHistory(peers=())
    outgoing = stored_message("outgoing-dm-no-to", 98, outbound=True)
    group = stored_message("group-incoming", 98, peer=GROUP)
    lid = stored_message("lid-incoming", 98, peer=LID)
    store.messages.extend([outgoing, group, lid])
    adapter = store.adapter()
    try:
        events = await adapter.poll()
        by_id = {event.provider_id: event for event in events}
        assert by_id[outgoing["id"]].peer == PHONE
        assert by_id[outgoing["id"]].direction == "outbound"
        assert by_id[group["id"]].peer == GROUP
        assert by_id[group["id"]].direction == "inbound"
        assert by_id[lid["id"]].metadata["canonical_peer"] == PHONE
        persist(database, events)
        clock.now += 3
        persist(database, await adapter.poll())
        # The chat index then catches up. Backfill replay must not add copies.
        store.peers = [PHONE, GROUP, LID]
        clock.now += 3
        persist(database, await adapter.poll())
    finally:
        await adapter.close()
    db, _ = database
    assert len(list(db.scalars(select(Message)))) == 3
    assert {chat.peer for chat in db.scalars(select(Conversation))} == {PHONE, GROUP}


@pytest.mark.asyncio
async def test_delayed_message_outside_global_tail_is_recovered_by_history_overlap(clock, database):
    store = StoredHistory()
    adapter = store.adapter()
    try:
        discovered = await adapter.poll()  # Empty history window ends at t=99.
        assert len(discovered) == 1 and discovered[0].metadata["conversation_only"]
        persist(database, discovered)
        assert list(database[0].scalars(select(Message))) == []
        assert database[0].scalar(select(Conversation)).peer == PHONE
        late = stored_message("late-outside-live-tail", 98)
        # Every newer record sorts ahead of `late` in the bounded newest page.
        newer = [stored_message(f"newer-{index}", 100)
                 for index in range(WhatsAppWebAdapter.PAGE_SIZE)]
        store.messages.extend([late, *newer])
        clock.now = 103
        events = await adapter.poll()
        assert any(event.provider_id == late["id"] for event in events), (
            "The live tail alone cannot recover this message; history must overlap its watermark"
        )
        persist(database, events)
    finally:
        await adapter.close()
    db, _ = database
    assert len(list(db.scalars(select(Message).where(Message.provider_id == late["id"])))) == 1


@pytest.mark.asyncio
async def test_global_tail_receipt_changes_update_existing_outbound_message(clock, database):
    store = StoredHistory(peers=())
    outgoing = stored_message("receipt-changes", 98, outbound=True)
    outgoing["ack"] = 1
    store.messages.append(outgoing)
    adapter = store.adapter()
    db, _ = database
    try:
        persist(database, await adapter.poll())
        assert db.scalar(select(Message)).status == "sent"
        outgoing["ack"] = 3
        clock.now += 3
        updated = await adapter.poll()
        assert any(event.provider_id == outgoing["id"] and event.status == "read"
                   for event in updated)
        persist(database, updated)
        clock.now += 3
        persist(database, await adapter.poll())
    finally:
        await adapter.close()
    messages = list(db.scalars(select(Message)))
    assert len(messages) == 1 and messages[0].status == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_commit", [True, False], ids=["committed", "rolled-back"])
async def test_tail_replays_after_adapter_recreation_and_deduplicates(clock, database, first_commit):
    store = StoredHistory(peers=())
    incoming = stored_message("replayed-after-restart", 98)
    store.messages.append(incoming)
    adapter = store.adapter()
    saved = copy.deepcopy(adapter.config)
    db, account = database
    try:
        first = await adapter.poll()
        assert any(event.provider_id == incoming["id"] for event in first)
        for event in first:
            ingest(db, account, event)
        if first_commit:
            db.commit()
            saved = copy.deepcopy(adapter.config)
        else:
            # A failed DB transaction keeps the pre-poll durable config. The
            # worker closes/discards the adapter, including its transient cache.
            db.rollback()
    finally:
        await adapter.close()
    assert len(list(db.scalars(select(Message)))) == int(first_commit)

    restored = store.adapter(saved)
    try:
        clock.now += 3
        replay = await restored.poll()
        assert any(event.provider_id == incoming["id"] for event in replay)
        persist(database, replay)
        persist(database, await restored.poll())
    finally:
        await restored.close()
    messages = list(db.scalars(select(Message)))
    assert len(messages) == 1 and messages[0].provider_id == incoming["id"]


@pytest.mark.asyncio
async def test_authoritative_raw_alias_change_is_replayed_despite_same_message_id(clock, database):
    store = StoredHistory(peers=())
    store.lid_pn = None
    incoming = stored_message("alias-resolved-later", 98, peer=LID)
    store.messages.append(incoming)
    adapter = store.adapter()
    db, _ = database
    try:
        persist(database, await adapter.poll())
        conversation = db.scalar(select(Conversation))
        conversation_id = conversation.id
        assert conversation.peer == LID and store.lid_lookups == 1

        # The provider later includes the authoritative PN/LID pair. Cache
        # suppression must not hide this identity change behind the same ID.
        incoming["_data"] = {"key": {
            "id": "alias-resolved-later", "fromMe": False,
            "remoteJid": LID.replace("@", ":2@"),
            "remoteJidAlt": PHONE.replace("@c.us", "@s.whatsapp.net"),
        }}
        clock.now += 3
        events = await adapter.poll()
        assert any(event.provider_id == incoming["id"]
                   and event.metadata["canonical_peer"] == PHONE for event in events)
        assert store.lid_lookups == 1  # Raw pairing makes another lookup unnecessary.
        persist(database, events)
    finally:
        await adapter.close()
    conversations = list(db.scalars(select(Conversation)))
    assert len(conversations) == 1
    assert conversations[0].id == conversation_id and conversations[0].peer == PHONE
    assert len(list(db.scalars(select(Message)))) == 1
