from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import aliased

from .adapters.base import Event
from .db import (Account, Audit, Conversation, ConversationInbox, ConversationRead,
                 Message, MessageInbox, PendingReceipt, now)


def account_for(db, account_id, user):
    account = db.get(Account, account_id)
    if account is None or account.tenant_id != user.tenant_id or (user.role != "admin" and account.owner_id != user.sub):
        raise HTTPException(404, "account_not_found")
    return account


def conversation_for(db, conversation_id, user):
    conversation = db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "conversation_not_found")
    account_for(db, conversation.account_id, user)
    return conversation


def lock_account_for(db, account_id, user):
    query = select(Account).where(Account.id == account_id, Account.tenant_id == user.tenant_id)
    if user.role != "admin":
        query = query.where(Account.owner_id == user.sub)
    account = db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if account is None:
        raise HTTPException(404, "account_not_found")
    return account


def audit(db, user, action, resource_id):
    db.add(Audit(tenant_id=user.tenant_id, actor=user.sub, action=action, resource_id=str(resource_id)))


def account_json(a):
    result = {k: getattr(a, k) for k in ("id", "channel", "label", "owner_id", "state", "public", "created_at")}
    if a.channel in {"whatsapp_cloud", "max_bot", "sms_gateway"}:
        result["public"] = {**(a.public or {}), "webhook_path": f"/webhooks/{a.channel}/{a.id}"}
    return result


def message_json(m, inbox_metadata=None):
    result = {k: getattr(m, k) for k in ("id", "conversation_id", "client_id", "provider_id", "text", "template", "direction", "status", "error", "created_at")}
    historical = bool(inbox_metadata.historical) if inbox_metadata is not None else m.direction == "inbound"
    result.update(historical=historical, read_eligible=m.direction == "inbound" and inbox_metadata is not None and not historical)
    return result


def conversation_metadata(db, conversation):
    row = db.get(ConversationInbox, conversation.id)
    if row is None:
        row = ConversationInbox(conversation_id=conversation.id, stage="new", favorite=False,
                                title_custom=False, search_text="")
        db.add(row)
    return row


def refresh_search(metadata, conversation, aliases=()):
    metadata.peer_aliases = list(dict.fromkeys(
        value for value in [*(metadata.peer_aliases or []), *aliases]
        if isinstance(value, str) and len(value) <= 256))[:100]
    values = [conversation.title, conversation.peer, *metadata.peer_aliases]
    # Unicode casefold is required for Cyrillic names on SQLite as well as PG.
    metadata.search_text = "\n".join(dict.fromkeys(str(value).casefold() for value in values if value))[:8192]


def record_message_sent(db, message, *, accepted_at=None):
    """Caller holds the account lock; only accepted sends stop response waiting."""
    if message.direction != "outbound" or message.status not in {"sent", "delivered", "read"}:
        return
    row = db.get(MessageInbox, message.id)
    if row is None:
        db.add(MessageInbox(message_id=message.id, historical=False, accepted_at=accepted_at or now()))
    elif accepted_at is None or row.accepted_at is None:
        row.accepted_at = accepted_at or now()


def discard_message_metadata(db, message):
    # PostgreSQL also enforces ON DELETE CASCADE; explicitly cover SQLite
    # deployments/tests where foreign-key enforcement may be disabled.
    db.execute(delete(MessageInbox).where(MessageInbox.message_id == message.id))


def inbox_page(db, user, *, q="", account_id=None, channel=None, limit=50, offset=0,
               conversation_id=None):
    query = select(Conversation.id).join(Account).where(Account.tenant_id == user.tenant_id)
    if user.role != "admin":
        query = query.where(Account.owner_id == user.sub)
    if account_id:
        account_for(db, account_id, user)
        query = query.where(Account.id == account_id)
    if channel:
        query = query.where(Account.channel == channel)
    if conversation_id:
        query = query.where(Conversation.id == conversation_id)
    query = query.outerjoin(ConversationInbox)
    if q.strip():
        term = q.strip().casefold()
        query = query.where(or_(
            ConversationInbox.search_text.contains(term, autoescape=True),
            func.lower(Conversation.title).contains(term, autoescape=True),
            func.lower(Conversation.peer).contains(term, autoescape=True),
            func.lower(Account.label).contains(term, autoescape=True),
        ))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    ids = query.order_by(Conversation.last_message_at.desc().nullslast(),
                         Conversation.created_at.desc(), Conversation.id).offset(offset).limit(limit)
    latest = (select(Message.id).where(Message.conversation_id == Conversation.id)
              .order_by(Message.created_at.desc(), Message.id.desc()).limit(1).correlate(Conversation).scalar_subquery())
    read_id = (select(ConversationRead.last_message_id).where(
        ConversationRead.conversation_id == Conversation.id, ConversationRead.user_id == user.sub)
        .correlate(Conversation).scalar_subquery())
    unread = (select(func.count(Message.id)).join(MessageInbox).where(
        Message.conversation_id == Conversation.id, Message.direction == "inbound",
        MessageInbox.historical.is_(False), Message.id > func.coalesce(read_id, 0))
        .correlate(Conversation).scalar_subquery())
    sent, sent_meta = aliased(Message), aliased(MessageInbox)
    accepted_at = func.coalesce(sent_meta.accepted_at, sent.created_at)
    replies = (select(accepted_at).select_from(sent).outerjoin(sent_meta, sent_meta.message_id == sent.id)
               .where(sent.conversation_id == Conversation.id, sent.direction == "outbound",
                      sent.status.in_(["sent", "delivered", "read"]))
               .order_by(accepted_at.desc(), sent.id.desc()).limit(1).correlate(Conversation))
    last_reply = replies.scalar_subquery()
    last_reply_id = replies.with_only_columns(sent.id).scalar_subquery()
    waiting = (select(func.min(Message.created_at)).join(MessageInbox).where(
        Message.conversation_id == Conversation.id, Message.direction == "inbound",
        MessageInbox.historical.is_(False), or_(last_reply.is_(None), Message.created_at > last_reply,
        and_(Message.created_at == last_reply, Message.id > last_reply_id)))
        .correlate(Conversation).scalar_subquery())
    newest = aliased(Message)
    rows = db.execute(select(
        Conversation, Account.channel, Account.label, Account.state,
        ConversationInbox.stage, ConversationInbox.favorite,
        newest.id, func.substr(newest.text, 1, 500), newest.created_at, unread, waiting,
    ).join(Account).outerjoin(ConversationInbox).outerjoin(newest, newest.id == latest)
        .where(Conversation.id.in_(ids))
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc(), Conversation.id))
    items = []
    for c, kind, label, state, stage, favorite, message_id, preview, activity, unread_count, waiting_since in rows:
        item = {key: getattr(c, key) for key in (
            "id", "account_id", "peer", "title", "last_message_at", "last_inbound_at", "created_at")}
        item.update(channel=kind, account_label=label, account_state=state, stage=stage or "new",
                    favorite=bool(favorite), last_message_id=message_id, preview=preview,
                    last_activity_at=activity, unread_count=unread_count, waiting_since=waiting_since)
        items.append(item)
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


def _next_status(current, status):
    """Receipts may be replayed and arrive out of order, including failures."""
    ranks = {"queued": 0, "sending": 1, "uncertain": 1, "sent": 2, "delivered": 3, "read": 4}
    if status == "failed":
        # A delayed error cannot undo evidence of delivery to the recipient.
        return current if current in {"delivered", "read"} else status
    if status not in ranks:
        return current
    if current == "failed":
        # Delivery evidence resolves an earlier error. A late send acknowledgement
        # alone cannot undo a provider's subsequent delivery failure.
        advance = status in {"delivered", "read"}
    else:
        advance = ranks[status] > ranks.get(current, -1)
    return status if advance else current


def advance_status(message, status):
    message.status = _next_status(message.status, status)
    if message.status in {"sent", "delivered", "read"}:
        message.error = None


def _store_receipt(db, account, event, canonical):
    pending = db.scalar(select(PendingReceipt).where(
        PendingReceipt.account_id == account.id,
        PendingReceipt.provider_id == event.provider_id,
        PendingReceipt.peer == canonical,
    ))
    if pending:
        pending.status = _next_status(pending.status, event.status)
    else:
        db.add(PendingReceipt(account_id=account.id, provider_id=event.provider_id,
                              peer=canonical, status=event.status))


def reconcile_receipts(db, account, message, peer):
    """Caller holds the account row lock; message provider ID is now known."""
    if not message.provider_id or message.direction != "outbound":
        return
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.account_id != account.id:
        return
    # The peer may have become canonical while the send was in flight.
    peers = {str(peer), conversation.peer}
    for pending in db.scalars(select(PendingReceipt).where(
            PendingReceipt.account_id == account.id,
            PendingReceipt.provider_id == message.provider_id,
            PendingReceipt.peer.in_(peers))):
        advance_status(message, pending.status)
        db.delete(pending)


def ingest(db, account, event: Event):
    """Caller serializes by locking account row. Cursors commit with these writes."""
    canonical = str(event.metadata.get("canonical_peer", event.peer))
    aliases = list({event.peer, canonical, *event.metadata.get("aliases", [])})
    conversations = list(db.scalars(select(Conversation).where(Conversation.account_id == account.id, Conversation.peer.in_(aliases))))
    c = next((c for c in conversations if c.peer == canonical), None)
    if c is None and conversations:
        c = next((c for c in conversations if c.peer == event.peer), min(conversations, key=lambda row: row.id))
    if len(conversations) > 1:
        # Do not merge possibly live outboxes implicitly. Keep both histories and
        # expose the conflict for review; subsequent canonical events stay stable.
        conflict = {"canonical_peer": canonical, "conversation_ids": sorted(row.id for row in conversations)}
        public = dict(account.public or {})
        conflicts = [item for item in public.get("identity_conflicts", []) if item.get("canonical_peer") != canonical]
        public["identity_conflicts"] = (conflicts + [conflict])[-20:]
        account.public = public
    if c is not None and c.peer != canonical:
        # Preserve the conversation ID referenced by the browser/outbox while
        # ensuring later events containing only the stable peer ID find it.
        c.peer = canonical
    if c is None and event.metadata.get("status_only"):
        _store_receipt(db, account, event, canonical)
        return
    if c is None:
        c = Conversation(account_id=account.id, peer=canonical, title=event.peer_name or event.peer)
        db.add(c)
        db.flush()
    metadata = conversation_metadata(db, c)
    if event.peer_name and not metadata.title_custom:
        # A provider profile/name can enrich discovery and historical replays;
        # explicit CRM titles are never overwritten by background sync.
        c.title = str(event.peer_name).strip()[:256] or c.title
    refresh_search(metadata, c, aliases)
    if event.metadata.get("conversation_only"):
        return
    existing = db.scalar(select(Message).where(Message.conversation_id == c.id, Message.provider_id == event.provider_id))
    # Status callbacks never create empty chat bubbles, and never regress read to sent.
    if event.metadata.get("status_only"):
        client_id = event.metadata.get("client_id")
        if not client_id and account.channel == "sms_gateway":
            # SMSGate accepts our UUID as its provider message identifier.
            client_id = event.provider_id
        if existing is None and isinstance(client_id, str):
            candidate = db.scalar(select(Message).where(
                Message.conversation_id == c.id, Message.client_id == client_id,
                Message.direction == "outbound"))
            if candidate is not None and candidate.provider_id in {None, event.provider_id}:
                existing = candidate
                existing.provider_id = event.provider_id
        if existing:
            advance_status(existing, event.status)
            record_message_sent(db, existing, accepted_at=(datetime.fromtimestamp(event.timestamp, timezone.utc)
                                if event.timestamp else aware(existing.created_at)))
            reconcile_receipts(db, account, existing, canonical)
        else:
            _store_receipt(db, account, event, canonical)
        return
    if existing:
        if existing.direction == "outbound":
            advance_status(existing, event.status)
            record_message_sent(db, existing, accepted_at=(datetime.fromtimestamp(event.timestamp, timezone.utc)
                                if event.timestamp else aware(existing.created_at)))
            reconcile_receipts(db, account, existing, canonical)
        return
    when = datetime.fromtimestamp(event.timestamp, timezone.utc) if event.timestamp else now()
    message = Message(conversation_id=c.id, provider_id=event.provider_id, text=event.text,
                      direction=event.direction, status=event.status, created_at=when)
    db.add(message)
    if not c.last_message_at or aware(c.last_message_at) < when:
        c.last_message_at = when
    if event.direction == "inbound" and not event.metadata.get("historical") and (not c.last_inbound_at or aware(c.last_inbound_at) < when):
        c.last_inbound_at = when
    db.flush()
    db.add(MessageInbox(message_id=message.id, historical=bool(event.metadata.get("historical")),
                        accepted_at=when if event.direction == "outbound" and event.status in {"sent", "delivered", "read"} else None))
    reconcile_receipts(db, account, message, canonical)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
