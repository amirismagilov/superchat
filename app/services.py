from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .adapters.base import Event
from .db import Account, Audit, Conversation, Message, PendingReceipt, now


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


def audit(db, user, action, resource_id):
    db.add(Audit(tenant_id=user.tenant_id, actor=user.sub, action=action, resource_id=str(resource_id)))


def account_json(a):
    result = {k: getattr(a, k) for k in ("id", "channel", "label", "owner_id", "state", "public", "created_at")}
    if a.channel in {"whatsapp_cloud", "max_bot", "sms_gateway"}:
        result["public"] = {**(a.public or {}), "webhook_path": f"/webhooks/{a.channel}/{a.id}"}
    return result


def message_json(m):
    return {k: getattr(m, k) for k in ("id", "conversation_id", "client_id", "provider_id", "text", "template", "direction", "status", "error", "created_at")}


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
            reconcile_receipts(db, account, existing, canonical)
        else:
            _store_receipt(db, account, event, canonical)
        return
    if existing:
        if existing.direction == "outbound":
            advance_status(existing, event.status)
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
    reconcile_receipts(db, account, message, canonical)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
