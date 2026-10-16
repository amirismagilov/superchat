/* Browser UI contract tests: all accounts, paging, explicit recipients and visible inbound read watermarks. No live providers. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const server = http.createServer((req, res) => {
    const file = req.url === '/' ? 'index.html' : path.basename(req.url);
    if (!['index.html', 'inbox.js', 'styles.css'].includes(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(__dirname, '../web', file)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  const errors = [], checks = [], requests = [], reads = [], mutations = [], actions = [];
  page.on('pageerror', error => errors.push(error.message));
  const accounts = [
    { id: 'a1', channel: 'telegram', label: 'Анна · продажи', state: 'connected', public: {} },
    { id: 'a2', channel: 'whatsapp_web', label: 'Рабочий WhatsApp', state: 'connected', public: {} },
    { id: 'a3', channel: 'max_personal', label: 'MAX компании', state: 'connected', public: {} },
    { id: 'a4', channel: 'telegram', label: 'Иван · продажи', state: 'connected', public: {} },
  ];
  const now = Date.now();
  const conversations = Array.from({ length: 55 }, (_, index) => {
    const account = accounts[index % accounts.length];
    return { id: `c${index + 1}`, account_id: account.id, channel: account.channel, account_label: account.label, account_state: account.state, title: index < 3 ? 'ООО Ромашка' : index === 3 ? 'Анна Смирнова' : index === 4 ? 'Стройкомплект' : `Клиент ${index + 1}`, peer: index === 0 ? '@romashka' : index === 1 ? '+79990000002' : index === 2 ? 'user:12345' : `client-${index + 1}`, preview: index === 0 ? 'Пришлите, пожалуйста, предложение на поставку' : index === 1 ? 'Спасибо! Мы получили документы.' : index === 2 ? 'Когда можно забрать заказ?' : 'Добрый день! Подскажите, пожалуйста, по заказу.', last_activity_at: new Date(now - index * 420000).toISOString(), last_message_at: new Date(now - index * 420000).toISOString(), unread_count: index === 0 ? 3 : index % 4 === 0 ? 1 : 0, stage: ['new', 'in_progress', 'done'][index % 3], favorite: index === 3, waiting_since: index % 3 === 0 ? new Date(now - index * 420000 - 300000).toISOString() : null, last_message_id: index === 0 ? 210 : index + 1000 };
  });
  const messages = Array.from({ length: 210 }, (_, index) => ({ id: index + 1, conversation_id: 'c1', direction: index % 2 ? 'inbound' : 'outbound', status: index % 2 ? 'received' : 'sent', read_eligible: !!(index % 2), historical: false, text: `Сообщение ${index + 1}`, created_at: new Date(now - (210 - index) * 60000).toISOString() }));
  let hideIncoming = false, delayedQuery = '';
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname, body = request.postDataJSON(); requests.push({ path: pathname, params: url.searchParams.toString(), method: request.method() });
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (pathname === '/api/runtime') return reply({ local_test: true, demo: false });
    if (pathname === '/api/local/token') return reply({ access_token: 'test-local' });
    if (pathname === '/api/me') return reply({ sub: 'local-owner', tenant_id: 'local-test', role: 'admin' });
    if (pathname === '/api/accounts') return reply(accounts);
    if (pathname === '/api/capabilities') return reply(accounts.slice(0, 3).map(account => ({ channel: account.channel, enabled: true, actions: ['login', 'history', 'disconnect'], connection: { configured: true, methods: ['qr', 'phone'] } })));
    if (pathname === '/api/inbox' || pathname === '/api/recipients') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      if (q && q === delayedQuery) await new Promise(resolve => setTimeout(resolve, 900));
      let found = conversations.filter(item => (!q || `${item.title} ${item.peer} ${item.account_label}`.toLowerCase().includes(q)) && (!url.searchParams.get('channel') || item.channel === url.searchParams.get('channel')) && (!url.searchParams.get('account_id') || item.account_id === url.searchParams.get('account_id')));
      const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 50);
      return reply({ items: found.slice(offset, offset + limit), total: found.length, has_more: offset + limit < found.length });
    }
    const conversationMatch = pathname.match(/^\/api\/conversations\/(c\d+)(?:\/(messages|read))?$/);
    if (conversationMatch) {
      const id = conversationMatch[1], action = conversationMatch[2], conversation = conversations.find(item => item.id === id);
      if (!action && request.method() === 'PATCH') { mutations.push({ id, ...body }); Object.assign(conversation, body); return reply(conversation); }
      if (action === 'read') {
        const message = messages.find(message => message.id === body.last_message_id);
        assert.equal(message?.direction, 'inbound', 'Only a visible inbound message may advance the read watermark');
        assert.equal(message?.read_eligible, true, 'Historical or unmarked messages cannot advance the read watermark');
        assert.equal(id, 'c1'); reads.push(body.last_message_id);
        conversation.unread_count = messages.filter(message => message.read_eligible === true && message.id > body.last_message_id).length;
        return reply({ conversation_id: id, last_read_message_id: body.last_message_id, unread_count: conversation.unread_count });
      }
      if (action === 'messages' && request.method() === 'POST') {
        const message = { id: 212, conversation_id: id, direction: 'outbound', status: 'queued', read_eligible: false, historical: false, text: body.text, created_at: new Date().toISOString() }; messages.push(message); return reply(message, 201);
      }
      if (action === 'messages') {
        let result = id === 'c1' ? messages.filter(message => !(hideIncoming && message.id === 211)) : [];
        const before = Number(url.searchParams.get('before')); if (before) result = result.filter(message => message.id < before);
        return reply(result.slice(-Number(url.searchParams.get('limit') || 100)));
      }
    }
    if (pathname === '/api/conversations' && request.method() === 'POST') {
      const account = accounts.find(account => account.id === body.account_id), item = { id: `c${conversations.length + 1}`, ...body, channel: account.channel, account_label: account.label, account_state: account.state, stage: 'new', unread_count: 0, favorite: false }; conversations.push(item); return reply(item, 201);
    }
    const accountMatch = pathname.match(/^\/api\/accounts\/(a\d+)\/actions$/);
    if (accountMatch) { const account = accounts.find(item => item.id === accountMatch[1]); actions.push({ account_id: account.id, ...body }); account.public.history_sync = { state: 'importing', history_days: body.payload.history_days, chats_discovered: 6, chats_completed: 2 }; return reply({ id: 'history-job' }, 202); }
    if (pathname === '/api/jobs/history-job') return reply({ id: 'history-job', status: 'done' });
    throw new Error(`Unexpected request ${request.method()} ${pathname}`);
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByRole('button', { name: 'Открыть локальный стенд', exact: true }).click();
    await page.getByText('1–50 из 55 диалогов', { exact: true }).waitFor();
    const table = page.getByRole('table', { name: 'Диалоги из всех мессенджеров' });
    assert.equal(await table.locator('tbody tr').count(), 50);
    assert.equal(requests.find(request => request.path === '/api/inbox').params.includes('account_id'), false);
    assert.equal(await table.getByText('ООО Ромашка', { exact: true }).count(), 3);
    assert.equal(await page.locator('img').count(), 0, 'Unknown client avatars use initials');
    await page.screenshot({ path: '/tmp/omnicrm-inbox-table.png', fullPage: true });
    await page.getByRole('button', { name: 'Далее', exact: true }).click(); await page.getByText('51–55 из 55 диалогов', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Назад', exact: true }).click(); await page.getByText('1–50 из 55 диалогов', { exact: true }).waitFor();
    await page.getByRole('combobox', { name: 'Мессенджер', exact: true }).selectOption('whatsapp_web');
    await page.waitForFunction(() => document.querySelector('omni-inbox')._conversations.length === 14);
    assert.equal(await table.getByText('WhatsApp', { exact: true }).count(), 14);
    await page.getByRole('combobox', { name: 'Мессенджер', exact: true }).selectOption('');
    await page.getByRole('searchbox', { name: 'Найти диалог' }).fill('ООО Ромашка');
    await page.getByText('1–3 из 3 диалогов', { exact: true }).waitFor();
    checks.push('All accessible accounts by default, same names stay separate, server paging/channel/search, initials');

    delayedQuery = 'задержка';
    const delayed = page.waitForRequest(request => new URL(request.url()).pathname === '/api/inbox' && new URL(request.url()).searchParams.get('q') === 'задержка');
    await page.getByRole('searchbox', { name: 'Найти диалог' }).fill('задержка'); await delayed;
    await page.getByRole('searchbox', { name: 'Найти диалог' }).fill('ООО Ромашка');
    await page.waitForTimeout(1200);
    assert.equal(await table.locator('tbody tr').count(), 3, 'Stale search results cannot overwrite the current query');
    delayedQuery = '';
    const first = table.locator('[data-conversation-id="c1"]');
    await first.getByRole('button', { name: 'В избранное: ООО Ромашка', exact: true }).click();
    await first.getByRole('button', { name: 'Убрать из избранного: ООО Ромашка', exact: true }).waitFor();
    await first.getByRole('combobox', { name: 'Этап: ООО Ромашка' }).selectOption('in_progress');
    await page.waitForFunction(() => document.querySelector('omni-inbox')._conversations.find(item => item.id === 'c1').stage === 'in_progress');
    assert.equal(await page.getByRole('log', { name: 'История сообщений' }).count(), 0, 'Row controls must not also open the conversation');
    checks.push('Stale query protection, persistent favorite and funnel stage without opening chat');

    await first.click();
    await page.getByRole('log').getByText('Сообщение 210', { exact: true }).waitFor();
    await page.waitForTimeout(250);
    assert.ok(reads.length && reads.at(-1) === 210);
    await page.screenshot({ path: '/tmp/omnicrm-inbox-chat.png', fullPage: true });
    const log = page.getByRole('log');
    await log.evaluate(node => { node.scrollTop = 0; });
    await log.getByRole('button', { name: 'Загрузить предыдущие сообщения' }).click();
    await log.getByText('Сообщение 11', { exact: true }).waitFor();
    assert.equal(await log.locator('[data-message-id]').count(), 200);
    messages.push({ id: 999, conversation_id: 'c1', direction: 'inbound', status: 'received', read_eligible: false, historical: true, text: 'История с большим ID', created_at: new Date().toISOString() });
    await page.evaluate(() => document.querySelector('omni-inbox').refresh());
    await log.evaluate(node => { node.scrollTop = node.scrollHeight; }); await page.waitForTimeout(250);
    assert.equal(reads.at(-1), 210, 'Visible historical backfill with a higher ID must not advance the read watermark');
    await log.evaluate(node => { node.scrollTop = 0; });
    messages.push({ id: 211, conversation_id: 'c1', direction: 'inbound', status: 'received', read_eligible: true, historical: false, text: 'Новое ещё не прочитанное', created_at: new Date().toISOString() }); conversations[0].unread_count = 1;
    await page.evaluate(() => document.querySelector('omni-inbox').refresh()); await page.waitForTimeout(250);
    assert.equal(reads.at(-1), 210, 'An off-screen incoming message must remain unread');
    hideIncoming = true;
    await page.getByRole('textbox', { name: 'Текст сообщения' }).fill('Ответ менеджера');
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await log.getByText('Ответ менеджера', { exact: true }).waitFor(); await page.waitForTimeout(250);
    assert.ok(reads.every(id => id !== 212), 'Outgoing optimistic message cannot acknowledge a newer inbound');
    hideIncoming = false; await page.evaluate(() => document.querySelector('omni-inbox').refresh());
    await log.getByText('Новое ещё не прочитанное', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Переименовать клиента' }).click();
    await page.getByRole('dialog').getByLabel('Имя клиента или компания *').fill('ООО Ландыш');
    await page.getByRole('dialog').getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByRole('heading', { name: 'ООО Ландыш', exact: true }).waitFor();
    await page.getByRole('button', { name: 'К списку диалогов' }).click();
    checks.push('Chat pane, older message paging, visible live inbound read watermark excludes historical/outgoing messages, rename');

    await page.getByRole('button', { name: 'Новый диалог', exact: true }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Клиент или компания').fill('ООО Ромашка');
    await dialog.getByText('Найдено диалогов: 2', { exact: true }).waitFor();
    assert.equal(await dialog.getByRole('button', { name: 'Открыть диалог', exact: true }).isDisabled(), true);
    await dialog.getByRole('combobox', { name: 'Мессенджер получателя' }).selectOption('max_personal');
    await dialog.getByText('Найдено диалогов: 1', { exact: true }).waitFor();
    await dialog.getByRole('combobox', { name: 'Аккаунт отправителя' }).selectOption('a3');
    await dialog.locator('.recipient-route').filter({ hasText: 'user:12345' }).click();
    await dialog.getByText('Открыть ООО Ромашка в MAX от аккаунта «MAX компании»', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: 'Открыть диалог', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('omni-inbox')._selectedConversation === 'c3');
    assert.equal(await page.evaluate(() => document.querySelector('omni-inbox')._selectedAccount), 'a3');
    assert.equal(requests.filter(request => request.path === '/api/conversations' && request.method === 'POST').length, 0);
    await page.getByRole('button', { name: 'К списку диалогов' }).click();
    await page.getByRole('button', { name: 'Новый диалог', exact: true }).first().click();
    await dialog.getByLabel('Клиент или компания').fill('ООО Ландыш'); await dialog.getByText('Найдено диалогов: 1', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: 'Новый получатель по номеру или username' }).click();
    await dialog.getByRole('combobox', { name: 'Мессенджер нового диалога' }).selectOption('telegram');
    await dialog.getByRole('combobox', { name: 'Рабочий аккаунт нового диалога' }).selectOption('a4');
    await dialog.getByLabel('Телефон или username клиента *').fill('@new_client');
    await dialog.getByLabel('Имя клиента или компания', { exact: true }).fill('');
    await dialog.getByRole('button', { name: 'Открыть диалог', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('omni-inbox')._selectedConversation === 'c56');
    assert.equal(conversations.at(-1).account_id, 'a4'); assert.equal(conversations.at(-1).peer, '@new_client');
    assert.equal(conversations.at(-1).title, '', 'A blank optional name must not pin the phone or username as a custom company name');
    checks.push('Recipient search across accounts, explicit channel and sender route, renamed recipient search, new peer uses chosen work account');

    await page.getByRole('button', { name: 'К списку диалогов' }).click();
    accounts[0].public.history_sync = { state: 'importing', history_days: 30, chats_discovered: 4, chats_completed: 1 };
    await page.getByRole('button', { name: 'Загрузить историю', exact: true }).first().click();
    await dialog.getByRole('combobox', { name: 'Аккаунт для загрузки истории' }).selectOption('a1');
    await dialog.getByRole('combobox', { name: 'Период истории' }).selectOption('3650');
    await dialog.getByRole('button', { name: 'Загрузить историю', exact: true }).click();
    await dialog.getByText('Обработано диалогов: 2 из 6', { exact: true }).waitFor();
    assert.deepEqual(actions.at(-1), { account_id: 'a1', action: 'history', payload: { history_days: 3650 } });
    assert.equal(await dialog.getByRole('button', { name: 'История загружается…', exact: true }).isDisabled(), true);
    accounts[0].public.history_sync = { state: 'complete', history_days: 3650, chats_discovered: 6, chats_completed: 6 };
    await page.evaluate(() => document.querySelector('omni-inbox').refresh());
    await dialog.getByText('Загрузка доступной истории завершена.', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: 'Закрыть', exact: true }).click();
    checks.push('Explicit history depth replaces shorter import, background progress and completion, same-period duplicate prevention');

    await page.getByRole('searchbox', { name: 'Найти диалог' }).fill(''); await page.getByText('1–50 из 56 диалогов', { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: '/tmp/omnicrm-inbox-mobile.png', fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await table.locator('[data-conversation-id="c2"]').click();
    await page.getByRole('heading', { name: 'ООО Ромашка', exact: true }).waitFor();
    await page.getByRole('button', { name: 'К списку диалогов' }).click();
    assert.deepEqual(errors, []);
    checks.push('Mobile table and chat back navigation, no page overflow or browser errors');
    console.log(JSON.stringify({ result: 'PASS', checks, screenshots: ['/tmp/omnicrm-inbox-table.png', '/tmp/omnicrm-inbox-chat.png', '/tmp/omnicrm-inbox-mobile.png'] }, null, 2));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
