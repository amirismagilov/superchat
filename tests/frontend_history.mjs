import assert from 'node:assert/strict';
import test from 'node:test';

// Exercise the real refresh method without a browser. Rendering is stubbed;
// account and message responses follow the API contract.
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
const { OmniInbox } = await import('../web/inbox.js');

function inboxWithMessages(initial, fresh) {
  const account = { id: 'account-1', state: 'connected' };
  const conversation = { id: 'conversation-1', account_id: account.id };
  const inbox = Object.create(OmniInbox.prototype);
  Object.assign(inbox, {
    isConnected: true,
    _identity: { sub: 'manager' },
    _generation: 0,
    _selectedAccount: account.id,
    _selectedConversation: conversation.id,
    _accounts: [account],
    _messages: initial,
    _connectionLabel: { textContent: '' },
    renderAccounts() {},
    async loadInbox() { this._conversations = [conversation]; },
    markVisibleMessagesRead() {},
    renderConversations() {},
    renderMessages() {},
    showError(message) { assert.fail(message); },
    async api(path) {
      if (path === '/api/accounts') return [account];
      if (path.startsWith('/api/conversations?')) return [conversation];
      return fresh;
    },
  });
  return inbox;
}

test('refresh keeps a newly imported older message before today across repeated polls', async () => {
  const today = { id: 1, text: 'Today', created_at: '2026-10-14T12:00:00Z' };
  const imported = { id: 2, text: 'Imported yesterday', created_at: '2026-10-13T12:00:00Z' };
  const inbox = inboxWithMessages([today], [imported, today]);
  await inbox.refresh();
  assert.deepEqual(inbox._messages.map(message => message.id), [2, 1]);
  await inbox.refresh();
  assert.deepEqual(inbox._messages.map(message => message.id), [2, 1]);
});

test('refresh preserves loaded older pages and uses numeric ids only for equal timestamps', async () => {
  const older = { id: 99, created_at: '2026-10-12T12:00:00Z' };
  const sameTimeA = { id: 3, created_at: '2026-10-14T12:00:00Z' };
  const sameTimeB = { id: 10, created_at: '2026-10-14T15:00:00+03:00' };
  const inbox = inboxWithMessages([older], [sameTimeB, sameTimeA]);
  await inbox.refresh();
  assert.deepEqual(inbox._messages.map(message => message.id), [99, 3, 10]);
});
