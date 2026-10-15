/* Real browser contract tests. Messenger providers are simulated; no credentials or messages leave the test. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const fixtureQR = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAZoAAAGaAQAAAAAefbjOAAADG0lEQVR4nO2bQW6kMBBFXw2WsjRSDpCjmBvkSK0caW4AR+kDjGQvI4FqFraB7tlM1BGQprxogZonlUXpu+pji/LlMfz6OgMGGWSQQQYZZNBzQlKGg0Ec+SqP5IBUH+h2Cc+g7aGgqqoRpPOfQtARhhak8yNAo6qqegttF55B20OpCkC4vqj2SYRwdTBUZVjrxg+Zk0HfBE0iHY1KB4DXf/vTnzcng/57uLt7HdpmhDQJMIkO75NT0l7hGbQ5VDPCK5QXPwmhH13OjXB9yRqxVoqDz8mgb4AGERFpQS6xUUgvKpcI0gHSMeVWY6/wDNpaI1YCMLSIzqsGMIneKsTh52TQ45B0QPEe/IjIm6p+vJXGk0FK17FTeAZtCJGNhh4Ar6q9Hym3QL7NVkS90v7gczLoEai87pDToslX+Y/eq6rGRvNVD5YRJ4BqRkTITiWUFICcIGMWiuVhy4gTQFkAPsTNaTGJ9v5TIDnkcnVZMqTbJTyDdlo1SrlQflQjFI0AVGNTV5eDz8mgR6C6EPjlxVOEItynilWWZ4CyHyEhToD/4xjeRwfpVQVASW3+Kb3pxuEZtDlUW0uYe85GVWuNuVSWuby0VeMsUNARueRlotSThDgVSypcHUBj+yPOAw1to5BEyCbl24h+tFDsynbn8Aza/LvG0AngFYHJEeKEhCwJkyPE1xGS1M8bB5+TQY9As0PV6OJdL5vsbloPqyPOAJX+YWgRFlEgvY6l6/CjK8pQ+4/Dz8mgR6DZoZr9CJ1th+pVFR/T/IhTQOuMWIyo3GnG6l9pXD9sGfHU0MqPKCkQgWxFxNs9+V7NxT4BVFVgNqvn9SOP2cpemdqWEc8MrTViPqhTTcpFPIo5ZavGaaC53VxvnsvGJUA92CXdPuEZtIMfAYspsa42qzwsZYVpxMkgrwrJoX1ySFdbD/1oJzsJfALo/kwXIYKE35Po0IEWFxuVENvtwzNoc+hmD9ViZftaVuQ/dH1A3FaNp4bue42SDNmwGlffNdZm5sHnZNAj0P2ZrnrVlL3Y+UDocrtxeAYZZJBBBhlk0IGhvxhUkNolWGxRAAAAAElFTkSuQmCC';
const caps = (configured = true) => [
  { channel: 'whatsapp_web', enabled: true, connection: { flow: 'qr', configured: true, methods: ['qr'] }, config_fields: [], actions: ['login', 'sync', 'disconnect'] },
  { channel: 'telegram', enabled: true, connection: { flow: 'phone_code', configured, methods: ['phone', 'qr'] }, config_fields: [{ name: 'phone', required: false }], actions: ['login', 'qr', 'code', 'password', 'sync', 'disconnect'] },
  { channel: 'sms_gateway', enabled: false, config_fields: [], actions: [] },
];
(async () => {
  const server = http.createServer((req, res) => {
    const file = req.url === '/' ? 'index.html' : path.basename(req.url);
    if (!['index.html', 'inbox.js', 'styles.css'].includes(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(__dirname, '../web', file)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  const errors = [], checks = [];
  async function fixture(options = {}) {
    const page = await browser.newPage({ viewport: options.mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 } });
    if (options.clock) await page.clock.install();
    page.on('pageerror', error => errors.push(error.message));
    const data = { accounts: options.accounts || [], actions: [], creates: [], jobs: new Map(), configured: options.configured !== false, mode: options.mode, jobReads: 0 };
    await page.route('**/api/**', async route => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname, body = request.postDataJSON();
      const reply = (json, status = 200) => route.fulfill({ json, status });
      if (pathname === '/api/runtime') return reply({ local_test: true, demo: false });
      if (pathname === '/api/local/token') return reply({ access_token: 'mock-session' });
      if (pathname === '/api/me') return reply({ sub: 'local-owner', tenant_id: 'local-test', role: options.role || 'admin' });
      if (pathname === '/api/capabilities') return reply(caps(data.configured));
      if (pathname === '/api/integrations/telegram') { if (request.method() === 'PUT') { assert.match(body.api_hash, /^[a-f0-9]{32}$/); data.configured = true; } return reply({ channel: 'telegram', configured: data.configured }); }
      if (pathname === '/api/accounts' && request.method() === 'GET') return reply(data.accounts);
      if (pathname === '/api/accounts' && request.method() === 'POST') {
        data.creates.push(body); const account = { id: 'account-' + (data.accounts.length + 1), channel: body.channel, label: body.label, state: 'new', public: {}, owner_id: 'local-owner' }; data.accounts.push(account); return reply(account, 201);
      }
      const accountMatch = pathname.match(/^\/api\/accounts\/([^/]+)(\/actions)?$/);
      if (accountMatch) {
        const account = data.accounts.find(a => a.id === accountMatch[1]);
        if (!accountMatch[2]) return reply(account);
        data.actions.push({ ...body, account_id: account.id });
        const id = 'job-' + data.actions.length;
        let error = null;
        if (body.action === 'login' && account.channel === 'telegram') { account.state = 'awaiting_code'; account.public = { phone: body.payload.phone || '+79991234567' }; }
        else if (body.action === 'login' || body.action === 'qr') {
          if (account.channel === 'whatsapp_web' && data.mode === 'connecting') { account.state = 'connecting'; account.public = { provider_state: 'STARTING' }; }
          else { account.state = 'qr_required'; account.public = { qr_data_url: fixtureQR, auth_method: 'qr', ...(account.channel === 'telegram' ? { expires_at: new Date(Date.now() + 60000).toISOString() } : {}) }; }
        }
        else if (body.action === 'code') {
          if (body.payload.code === '00000') error = 'telegram_code_invalid';
          else { account.state = 'awaiting_password'; account.public = { phone: '+79991234567' }; }
        } else if (body.action === 'password') {
          if (body.payload.password === 'wrong') error = 'telegram_password_invalid';
          else { account.state = 'connected'; account.public = { identity: { phone: '+79991234567', display_name: 'Тестовый менеджер' } }; }
        } else if (body.action === 'disconnect') { account.state = 'disconnected'; account.public = {}; }
        if (data.mode === 'tg_queued' && account.channel === 'telegram') {
          account.state = 'new'; account.public = { error: 'telegram_transport_error' };
        }
        data.jobs.set(id, { id, status: data.mode === 'tg_queued' ? 'queued' : data.mode === 'pending' ? 'running' : error ? 'failed' : 'done', error });
        return reply({ id }, 202);
      }
      if (pathname.startsWith('/api/jobs/')) { data.jobReads++; return reply(data.jobs.get(pathname.split('/').pop())); }
      if (pathname === '/api/conversations') return reply([]);
      throw new Error('Unhandled mock API: ' + request.method() + ' ' + pathname);
    });
    await page.goto('http://127.0.0.1:' + server.address().port);
    await page.getByRole('button', { name: 'Открыть локальный стенд', exact: true }).click();
    if (options.mobile) await page.getByRole('button', { name: 'Выбрать канал', exact: true }).click();
    await page.getByRole('button', { name: 'Подключить канал', exact: true }).first().click();
    return { page, data, dialog: page.getByRole('dialog'), async refresh() { await page.evaluate(() => document.querySelector('omni-inbox').refresh()); } };
  }
  try {
    const wa = await fixture();
    await wa.dialog.getByRole('button', { name: 'Подключить WhatsApp', exact: true }).click();
    await wa.dialog.getByRole('img', { name: 'QR-код подключения WhatsApp' }).waitFor();
    assert.equal(await wa.dialog.locator('.connection-steps li').count(), 4);
    assert.equal(wa.data.actions.length, 1);
    assert.equal(await wa.dialog.getByText(/подключён$/).count(), 0);
    assert.equal(await wa.dialog.locator('input').count(), 0);
    await wa.page.screenshot({ path: '/tmp/omnicrm-connection-wa.png' });
    wa.data.accounts[0].state = 'connected'; wa.data.accounts[0].public = { identity: { phone: '+79990001122' } };
    await wa.refresh();
    await wa.dialog.getByText('Канал +79990001122 подключён', { exact: true }).waitFor();
    await wa.dialog.getByRole('button', { name: 'Перейти в чаты' }).click();
    assert.equal(await wa.page.getByRole('dialog').count(), 0);
    assert.equal(wa.data.actions.length, 1, 'QR updates must not enqueue redundant sync jobs');
    checks.push('WA QR instructions, no technical fields, automatic verified success, no extra auth jobs');
    await wa.page.close();

    const backendTimeout = await fixture();
    await backendTimeout.dialog.getByRole('button', { name: 'Подключить WhatsApp', exact: true }).click();
    await backendTimeout.dialog.getByRole('img', { name: 'QR-код подключения WhatsApp' }).waitFor();
    backendTimeout.data.accounts[0].state = 'action_required';
    backendTimeout.data.accounts[0].public = { error: 'waha_connect_timeout', provider_state: 'STARTING' };
    await backendTimeout.refresh();
    await backendTimeout.dialog.getByRole('alert').getByText(/WhatsApp не завершил подключение за 2 минуты/).waitFor();
    assert.equal(await backendTimeout.dialog.locator('.connection-spinner').count(), 0);
    await backendTimeout.dialog.getByRole('button', { name: 'Подключить снова', exact: true }).click();
    await backendTimeout.dialog.getByRole('img', { name: 'QR-код подключения WhatsApp' }).waitFor();
    assert.deepEqual(backendTimeout.data.actions.map(action => action.action), ['login', 'login']);
    assert.equal(backendTimeout.data.creates.length, 1, 'Retry must reuse the existing corporate account');
    assert.equal(await backendTimeout.dialog.getByRole('alert').count(), 0);
    checks.push('WA server deadline removes spinner, explicit retry logs in again on the same account');
    await backendTimeout.page.close();

    const stalled = await fixture({ mode: 'connecting', clock: true });
    await stalled.dialog.getByRole('button', { name: 'Подключить WhatsApp', exact: true }).click();
    await stalled.dialog.getByRole('button', { name: 'Обновить QR-код', exact: true }).waitFor();
    await stalled.page.clock.fastForward(121000);
    await stalled.dialog.getByRole('alert').getByText(/Сервер не вернул QR-код за 2 минуты/).waitFor();
    assert.equal(await stalled.dialog.locator('.connection-spinner').count(), 0);
    assert.equal(stalled.data.actions.length, 1, 'Client deadline must not automatically restart the account');
    assert.equal(stalled.data.accounts[0].state, 'connecting', 'Client deadline must not fabricate provider state');
    await stalled.page.screenshot({ path: '/tmp/omnicrm-wa-qr-timeout.png' });
    stalled.data.accounts[0].state = 'qr_required'; stalled.data.accounts[0].public = { qr_data_url: fixtureQR };
    await stalled.refresh();
    await stalled.dialog.getByRole('img', { name: 'QR-код подключения WhatsApp' }).waitFor();
    assert.equal(await stalled.dialog.getByRole('alert').count(), 0, 'A later valid QR clears the client deadline notice');
    stalled.data.accounts[0].state = 'connecting'; stalled.data.accounts[0].public = { error: 'waha_session_failed', provider_state: 'FAILED' };
    await stalled.refresh();
    await stalled.dialog.getByRole('alert').getByText(/Сеанс WhatsApp завершился с ошибкой/).waitFor();
    assert.equal(await stalled.dialog.locator('.connection-spinner').count(), 0, 'Provider errors must not claim that QR generation is progressing');
    stalled.data.mode = null;
    await stalled.dialog.getByRole('button', { name: 'Повторить подключение', exact: true }).click();
    await stalled.dialog.getByRole('img', { name: 'QR-код подключения WhatsApp' }).waitFor();
    assert.deepEqual(stalled.data.actions.map(action => action.action), ['login', 'login']);
    assert.equal(stalled.data.creates.length, 1);
    assert.equal(await stalled.dialog.getByRole('alert').count(), 0);
    checks.push('WA client 120s watchdog, no fake diagnosis or state change, late QR recovery, FAILED retry uses login');
    await stalled.page.close();

    const pendingWait = await fixture({ mode: 'pending', clock: true });
    await pendingWait.dialog.getByRole('button', { name: 'Подключить WhatsApp', exact: true }).click();
    await pendingWait.dialog.getByText('Мессенджер подтверждает подключение…').waitFor();
    pendingWait.data.accounts[0].state = 'connecting'; pendingWait.data.accounts[0].public = { provider_state: 'STARTING' };
    await pendingWait.page.clock.fastForward(121000);
    await pendingWait.dialog.getByText('Мессенджер подтверждает подключение…').waitFor();
    assert.equal(await pendingWait.dialog.getByText(/Сервер не вернул QR-код за 2 минуты/).count(), 0, 'The watchdog must not replace an ongoing auth job');
    assert.equal(pendingWait.data.actions.length, 1);
    checks.push('WA watchdog leaves the known ongoing job intact');
    await pendingWait.page.close();

    const tg = await fixture();
    await tg.dialog.getByRole('button', { name: 'Подключить Telegram', exact: true }).click();
    await tg.dialog.getByLabel('Номер телефона *', { exact: true }).fill('+7 (999) 123-45-67');
    await tg.dialog.getByRole('button', { name: 'Получить код', exact: true }).dblclick();
    await tg.dialog.getByLabel('Код из сообщения *').fill('00000');
    assert.equal(tg.data.creates.length, 1); assert.equal(tg.data.actions.length, 1);
    assert.deepEqual(tg.data.creates[0].config, { phone: '+79991234567' });
    await tg.dialog.getByRole('button', { name: 'Подключить', exact: true }).click();
    await tg.dialog.getByRole('alert').getByText(/Код не подошёл/).waitFor();
    await tg.dialog.getByLabel('Код из сообщения *').fill('12345');
    await tg.dialog.getByRole('button', { name: 'Подключить', exact: true }).click();
    await tg.dialog.getByLabel('Пароль двухэтапной проверки *').fill('wrong');
    await tg.dialog.getByRole('button', { name: 'Подтвердить пароль' }).click();
    await tg.dialog.getByRole('alert').getByText(/Неверный пароль/).waitFor();
    await tg.dialog.getByLabel('Пароль двухэтапной проверки *').fill('example-only');
    await tg.dialog.getByRole('button', { name: 'Подтвердить пароль' }).click();
    await tg.dialog.getByText('Канал +79991234567 подключён', { exact: true }).waitFor();
    await tg.page.screenshot({ path: '/tmp/omnicrm-connection-success.png' });
    assert.equal(await tg.dialog.locator('input').count(), 0);
    assert.deepEqual(await tg.page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });
    checks.push('TG phone/code/2FA, double submit guard, invalid code and password recovery, no credential storage');
    await tg.page.close();

    const tgQueued = await fixture({ mode: 'tg_queued', clock: true });
    await tgQueued.dialog.getByRole('button', { name: 'Подключить Telegram', exact: true }).click();
    await tgQueued.dialog.getByLabel('Номер телефона *', { exact: true }).fill('+79991234567');
    await tgQueued.dialog.getByRole('button', { name: 'Получить код', exact: true }).click();
    await tgQueued.dialog.getByText('Сервер готовит подключение…', { exact: true }).waitFor();
    await tgQueued.refresh();
    await tgQueued.dialog.getByRole('alert').getByText(/Сервер не смог установить связь с Telegram/).waitFor();
    await tgQueued.page.clock.fastForward(136000);
    await tgQueued.page.clock.runFor(1600);
    await tgQueued.dialog.getByRole('alert').getByText(/Сервер пока не начал подключение/).waitFor();
    assert.equal(await tgQueued.dialog.getByRole('button', { name: 'Получить код', exact: true }).isDisabled(), true);
    assert.equal(tgQueued.data.actions.length, 1, 'Uncertain queue timeout must not enqueue a duplicate');
    tgQueued.data.jobs.get('job-1').status = 'failed';
    tgQueued.data.jobs.get('job-1').error = 'telegram_transport_error';
    await tgQueued.dialog.getByRole('button', { name: 'Проверить выполнение запроса', exact: true }).click();
    await tgQueued.dialog.getByRole('alert').getByText(/Сервер не смог установить связь с Telegram/).waitFor();
    assert.equal(await tgQueued.dialog.getByRole('button', { name: 'Получить код', exact: true }).isEnabled(), true);
    tgQueued.data.mode = null;
    await tgQueued.dialog.getByRole('button', { name: 'Получить код', exact: true }).click();
    await tgQueued.dialog.getByLabel('Код из сообщения *').waitFor();
    assert.equal(tgQueued.data.creates.length, 1, 'Telegram retry must retain the existing corporate channel');
    assert.equal(tgQueued.data.actions.length, 2);
    checks.push('TG queued transport error is visible during wait, wall-clock deadline retains pending job, terminal failure allows retry on same account');
    await tgQueued.page.close();

    const qr = await fixture({ mobile: true });
    await qr.dialog.getByRole('button', { name: 'Подключить Telegram', exact: true }).click();
    await qr.dialog.getByRole('button', { name: 'Подключить по QR-коду' }).click();
    await qr.dialog.getByRole('img', { name: 'QR-код подключения Telegram' }).waitFor();
    await qr.page.screenshot({ path: '/tmp/omnicrm-connection-mobile.png' });
    assert.equal(await qr.page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    qr.data.accounts[0].public.expires_at = new Date(Date.now() - 1000).toISOString(); await qr.refresh();
    assert.equal(await qr.dialog.getByRole('img').count(), 0, 'Expired auth QR must not remain scannable');
    await qr.dialog.getByRole('button', { name: 'Войти по номеру телефона' }).click();
    await qr.dialog.getByLabel('Номер телефона *', { exact: true }).fill('+79990000000');
    await qr.refresh();
    assert.equal(await qr.dialog.getByLabel('Номер телефона *', { exact: true }).inputValue(), '+79990000000');
    checks.push('TG real QR action, provider expiry, mobile layout, switch QR to phone without stale refresh');
    await qr.page.close();

    const close = await fixture({ mode: 'pending' });
    await close.dialog.getByRole('button', { name: 'Подключить WhatsApp', exact: true }).click();
    await close.dialog.getByText('Мессенджер подтверждает подключение…').waitFor();
    await close.dialog.getByRole('button', { name: 'Закрыть окно' }).click();
    const reads = close.data.jobReads;
    await close.page.waitForTimeout(1700);
    assert.equal(close.data.jobReads, reads, 'Closing modal cancels future job polling');
    assert.equal(await close.page.getByRole('dialog').count(), 0);
    checks.push('Close stops job polling and does not reopen modal');
    await close.page.close();

    const admin = await fixture({ configured: false });
    await admin.dialog.getByRole('button', { name: 'Подключить Telegram', exact: true }).click();
    await admin.dialog.getByRole('button', { name: 'Настроить Telegram', exact: true }).click();
    await admin.dialog.getByLabel('Telegram API ID *').fill('123456');
    await admin.dialog.getByLabel('Telegram API hash *').fill('a'.repeat(32));
    await admin.dialog.getByRole('button', { name: 'Сохранить и продолжить' }).click();
    await admin.dialog.getByLabel('Номер телефона *', { exact: true }).waitFor();
    assert.equal(await admin.dialog.getByLabel('Telegram API hash *').count(), 0);
    checks.push('Admin setup is separate and returns to manager flow');
    await admin.page.close();

    const manager = await fixture({ configured: false, role: 'manager' });
    await manager.dialog.getByRole('button', { name: 'Подключить Telegram', exact: true }).click();
    await manager.dialog.getByText('Нужна настройка подключения', { exact: true }).waitFor();
    assert.equal(await manager.dialog.locator('input').count(), 0);
    assert.equal(await manager.dialog.getByRole('button', { name: 'Настроить Telegram', exact: true }).count(), 0);
    checks.push('Manager cannot configure shared application credentials');
    await manager.page.close();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', checks, screenshots: ['/tmp/omnicrm-connection-wa.png', '/tmp/omnicrm-connection-success.png', '/tmp/omnicrm-connection-mobile.png'] }, null, 2));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
