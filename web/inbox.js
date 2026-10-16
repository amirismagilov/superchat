/* Buildless, isolated CRM inbox. Authentication lives only in this element's memory. */
const STYLE_URL = new URL('./styles.css', import.meta.url).href;
const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
  chat: ['M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z'],
  plus: ['M12 5v14', 'M5 12h14'],
  search: ['M21 21l-4.4-4.4', 'M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0'],
  close: ['M18 6 6 18', 'm6 6 12 12'],
  arrow: ['m12 19-7-7 7-7', 'M5 12h14'],
  send: ['m22 2-7 20-4-9-9-4Z', 'M22 2 11 13'],
  settings: ['M4 7h16', 'M4 17h16', 'M8 4v6', 'M16 14v6'],
  check: ['m20 6-11 11-5-5'],
  checks: ['m18 6-7 8-3-3', 'm22 10-7 8-3-3', 'm5 14-3-3'],
  lock: ['M6 10V7a6 6 0 0 1 12 0v3', 'M4 10h16v11H4z'],
  logout: ['M9 5H4v14h5', 'M10 12h11', 'm17 8 4 4-4 4'],
  refresh: ['M20 11a8 8 0 0 0-14-5L3 9', 'M3 3v6h6', 'M4 13a8 8 0 0 0 14 5l3-3', 'M21 21v-6h-6'],
  inbox: ['M4 4h16l2 12v4H2v-4z', 'M2 14h6l2 3h4l2-3h6'],
  warning: ['m12 3 10 18H2Z', 'M12 9v4', 'M12 17h.01'],
  star: ['m12 3 2.8 5.7 6.3.9-4.6 4.4 1.1 6.3-5.6-3-5.6 3 1.1-6.3L3 9.6l6.2-.9Z'],
  clock: ['M12 8v5l3 2', 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0'],
  edit: ['m16 3 5 5-12 12-6 1 1-6Z', 'm14 5 5 5'],
};
const CHANNEL_LABELS = { telegram: 'Telegram', whatsapp_cloud: 'WhatsApp Business', whatsapp_web: 'WhatsApp', max_bot: 'MAX · бот', max_personal: 'MAX', sms_gateway: 'SMS', demo: 'Демо-канал' };
const CHANNEL_INITIALS = { telegram: '↗', whatsapp_cloud: 'W', whatsapp_web: 'W', max_bot: 'M', max_personal: 'M', sms_gateway: 'SMS', demo: 'D' };
const MESSAGE_STATUSES = { queued: 'В очереди', sending: 'Отправляется', sent: 'Отправлено', delivered: 'Доставлено', read: 'Прочитано', failed: 'Не отправлено', uncertain: 'Доставка не подтверждена', received: '' };
const STATE_LABELS = { connected: 'Подключён', ready: 'Подключён', authorized: 'Подключён', active: 'Подключён', working: 'Подключён', disconnected: 'Отключён', offline: 'Не в сети', error: 'Ошибка', failed: 'Ошибка', created: 'Нужно подключить', new: 'Нужно подключить', pending: 'Ожидает подключения', pending_login: 'Ожидает входа', needs_login: 'Нужен вход', login_required: 'Нужен вход', need_code: 'Ожидает код', code_required: 'Ожидает код', awaiting_code: 'Ожидает код', need_password: 'Ожидает пароль', password_required: 'Ожидает пароль', awaiting_password: 'Ожидает пароль', qr: 'Нужно отсканировать QR', qr_required: 'Нужно отсканировать QR', scan_qr_code: 'Нужно отсканировать QR', needs_qr: 'Нужно отсканировать QR', starting: 'Подключается', connecting: 'Подключается', awaiting_qr: 'Ожидает QR-код', reauth_required: 'Нужен повторный вход', action_required: 'Требуется действие' };

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2), value);
    else if (key === 'class') element.className = value;
    else if (key === 'text') element.textContent = value;
    else if (key === 'hidden' || key === 'disabled' || key === 'required' || key === 'checked') element[key] = !!value;
    else if (value !== null && value !== undefined) element.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function icon(name, size = 17) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  for (const [key, value] of Object.entries({ width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.65, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
  for (const d of ICONS[name] || ICONS.chat) { const p = document.createElementNS(SVG_NS, 'path'); p.setAttribute('d', d); svg.append(p); }
  return svg;
}
function button(text, action, variant = '', iconName = null) { return el('button', { type: 'button', class: `button ${variant}`, onclick: action }, [iconName ? icon(iconName, 15) : null, text]); }
function iconButton(name, label, action, extraClass = '') { return el('button', { type: 'button', class: `icon-button ${extraClass}`, 'aria-label': label, title: label, onclick: action }, icon(name)); }
function channelIcon(channel) {
  const mark = el('span', { class: `channel-icon ${channel}`, 'aria-hidden': 'true' });
  if (channel === 'telegram' || channel.startsWith('whatsapp')) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '22'); svg.setAttribute('height', '22'); svg.setAttribute('fill', 'currentColor');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', channel === 'telegram' ? 'M21.8 3.5 18.5 20c-.2 1.1-.9 1.4-1.8.9l-5-3.7-2.4 2.3c-.3.3-.5.5-1 .5l.4-5.1L18 6.4c.4-.4-.1-.6-.6-.3L5.9 13.4 1 11.9c-1.1-.3-1.1-1.1.2-1.6L20.4 2.9c.9-.3 1.7.2 1.4.6Z' : 'M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 1.8a8.2 8.2 0 1 1-4.2 15.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 0 1 12 3.8Zm-3 3.5c-.2 0-.5.1-.7.3-.3.3-.9.9-.9 2.1s.9 2.4 1 2.6 1.8 2.8 4.4 3.9c2.2.9 2.6.6 3.1.6s1.6-.7 1.8-1.4.2-1.3.1-1.4-.3-.2-.6-.4l-1.9-.9c-.2-.1-.4-.1-.6.2l-.8 1c-.2.2-.4.2-.6.1a6.6 6.6 0 0 1-3.4-3c-.2-.3 0-.4.1-.5l.5-.6.2-.4c.1-.2 0-.4 0-.5l-.9-2c-.2-.5-.4-.5-.6-.5Z');
    svg.append(path); mark.append(svg);
  } else mark.textContent = CHANNEL_INITIALS[channel] || '•';
  return mark;
}
function connectionError(error) {
  const code = error?.message || String(error || '');
  const messages = {
    telegram_code_invalid: 'Код не подошёл. Проверьте последнее сообщение от Telegram и попробуйте ещё раз.',
    telegram_code_expired: 'Срок действия кода истёк. Запросите новый код.',
    telegram_password_invalid: 'Неверный пароль двухэтапной проверки. Попробуйте ещё раз.',
    telegram_phone_invalid: 'Введите номер в международном формате, например +79991234567.',
    telegram_phone_banned: 'Telegram ограничил вход для этого номера. Проверьте аккаунт в приложении Telegram.',
    telegram_flood_wait: 'Telegram просит подождать перед следующей попыткой. Повторите подключение позднее.',
    telegram_login_required: 'Запросите новый код для входа в Telegram.',
    telegram_session_expired: 'Сеанс Telegram завершён. Войдите в аккаунт ещё раз.',
    telegram_qr_expired: 'QR-код устарел. Получите новый код и отсканируйте его.',
    telegram_qr_invalid: 'QR-код больше не действует. Получите новый код.',
    telegram_app_not_configured: 'Администратор ещё не настроил подключение Telegram для команды.',
    telegram_configuration_invalid: 'Настройки Telegram требуют проверки администратором.',
    telegram_transport_error: 'Сервер не смог установить связь с Telegram. Повторите вход. Если ошибка повторится, администратору нужно проверить доступ сервера к Telegram.',
    telegram_server_error: 'Telegram временно не ответил. Повторите вход немного позже.',
    provider_auth_timeout: 'Подключение не завершилось вовремя. Повторите вход. Если ожидание повторится, попросите администратора проверить связь сервера с мессенджером.',
    worker_interrupted_retry_login: 'Во время подключения сервер перезапустился. Повторите вход.',
    telegram_account_identity_changed: 'В этом канале был другой аккаунт. Для нового номера добавьте отдельный канал.',
    whatsapp_account_identity_changed: 'В этом канале был другой аккаунт. Для нового номера добавьте отдельный канал.',
    max_account_identity_changed: 'В этом канале был другой аккаунт MAX. Для нового аккаунта добавьте отдельный канал.',
    max_phone_invalid: 'Введите номер в международном формате, например +79991234567.',
    max_phone_required: 'Введите номер вашего рабочего аккаунта MAX в международном формате.',
    max_code_invalid: 'Код не подошёл. Проверьте последний код входа в MAX и попробуйте ещё раз.',
    invalid_max_code: 'Код не подошёл. Введите цифры из последнего кода входа в MAX.',
    max_code_expired: 'Срок действия кода MAX истёк. Запросите новый код.',
    max_password_invalid: 'Неверный пароль MAX. Проверьте пароль и попробуйте ещё раз.',
    max_password_required: 'Введите пароль двухэтапной проверки MAX.',
    max_qr_expired: 'QR-код MAX устарел. Получите новый код и подтвердите вход на телефоне.',
    max_auth_action_out_of_sequence: 'Этот шаг входа в MAX больше не действует. Начните подключение ещё раз.',
    max_transport_error: 'Сервер не смог установить связь с MAX. Повторите вход немного позже.',
    max_auth_failed: 'MAX не подтвердил вход. Повторите подключение и подтвердите вход на телефоне.',
    max_session_expired: 'Сеанс MAX завершён. Войдите в аккаунт ещё раз.',
    max_restore_failed: 'Не удалось восстановить подключение MAX. Повторите вход.',
    max_credentials_rejected_or_registration_required: 'MAX не подтвердил существующий аккаунт. Проверьте вход в приложении MAX на телефоне и повторите подключение.',
    max_revoke_unconfirmed: 'MAX пока не подтвердил отключение аккаунта. Повторите попытку немного позже.',
    max_own_profile_missing: 'MAX ещё не подтвердил профиль аккаунта. Подождите или повторите подключение.',
    install_maxapi_python_2_4_1: 'Подключение MAX ещё не подготовлено на сервере. Обратитесь к администратору.',
    waha_transport_error: 'Нет связи со шлюзом WhatsApp. Повторите проверку через некоторое время.',
    waha_server_error: 'Шлюз WhatsApp временно недоступен. Повторите проверку через некоторое время.',
    waha_rate_limited: 'WhatsApp просит подождать перед следующей попыткой.',
    waha_request_rejected: 'WhatsApp не подтвердил запрос. Проверьте связанные устройства на телефоне и повторите попытку.',
    waha_connect_timeout: 'Подтверждение WhatsApp занимает больше двух минут. Продолжаем проверять подключение. При необходимости можно повторить попытку.',
    waha_session_failed: 'Сеанс WhatsApp завершился с ошибкой. Повторите подключение.',
    waha_session_stopped: 'Сеанс WhatsApp остановлен. Повторите подключение, чтобы запустить его снова.',
    auth_job_expired: 'Запрос входа устарел. Начните подключение ещё раз.',
  };
  return messages[code] || (/^[a-z][a-z0-9_]+$/.test(code) ? 'Не удалось завершить подключение. Повторите проверку или обратитесь к администратору.' : code);
}
function initials(value) { return String(value || '?').split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map(v => v[0]).join('').toUpperCase(); }
function array(value) { return Array.isArray(value) ? value : value?.items || []; }
function date(value) { if (!value) return null; const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
function timeLabel(value) { return date(value)?.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) || ''; }
function dateLabel(value) { return date(value)?.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }) || ''; }
function shortDateLabel(value) { const d = date(value); if (!d) return ''; return d.toDateString() === new Date().toDateString() ? 'Сегодня' : d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }); }
function waitingLabel(value) { const elapsed = Math.max(0, Math.floor((Date.now() - (date(value)?.getTime() || Date.now())) / 60000)); return elapsed < 1 ? 'Только что' : elapsed < 60 ? `${elapsed} мин` : elapsed < 1440 ? `${Math.floor(elapsed / 60)} ч ${elapsed % 60} мин` : `${Math.floor(elapsed / 1440)} д`; }
function stateLabel(state) { return STATE_LABELS[String(state || '').toLowerCase()] || state || 'Не подключён'; }
function stateColor(state) { const s = String(state || '').toLowerCase(); return /^(connected|ready|authorized|active|working)$/.test(s) ? 'green' : /error|failed|disconnected/.test(s) ? 'red' : 'orange'; }
function modeBadge(capability, channel) {
  const mode = capability?.mode || (channel === 'demo' ? 'demo' : 'unknown');
  const label = mode === 'demo' ? 'ДЕМО' : /official/.test(mode) && !/unofficial/.test(mode) ? 'ОФИЦИАЛЬНЫЙ API' : /experimental|unofficial|personal|web/.test(mode) ? 'ЭКСПЕРИМЕНТАЛЬНО' : mode;
  const className = mode === 'demo' ? 'demo' : label === 'ОФИЦИАЛЬНЫЙ API' ? 'official' : 'experimental';
  return el('span', { class: `badge ${className}` }, label);
}
function emptyState(title, description, action = null, small = false) { return el('div', { class: `empty${small ? ' small' : ''}` }, [small ? null : el('div', { class: 'empty-icon' }, icon('chat', 27)), el('h3', {}, title), el('p', {}, description), action]); }
function uuid() { if (globalThis.crypto?.randomUUID) return crypto.randomUUID(); const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128; const h = Array.from(bytes, v => v.toString(16).padStart(2, '0')).join(''); return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`; }

export class OmniInbox extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._root = el('div');
    this.shadowRoot.append(el('link', { rel: 'stylesheet', href: STYLE_URL }), this._root);
    this._token = '';
    this._baseUrl = '';
    this._generation = 0;
    this._accounts = [];
    this._capabilities = [];
    this._conversations = [];
    this._messages = [];
    this._drafts = new Map();
    this._pendingSends = new Map();
    this._jobs = new Set();
    this._connectionJobs = new Map();
    this._filterAccountId = ''; this._filterChannel = ''; this._inboxQuery = ''; this._inboxOffset = 0; this._inboxPageSize = 50; this._inboxRequest = 0; this._inboxTotal = 0; this._inboxHasMore = false;
    this._activeConversation = null; this._conversationMutations = new Set(); this._lastRead = new Map(); this._reading = new Set(); this._historyJobs = new Map();
    this._session = new AbortController();
    this._visibility = () => { if (!document.hidden && this._identity) this.refresh(); };
  }
  connectedCallback() { if (this._session.signal.aborted) this._session = new AbortController(); document.addEventListener('visibilitychange', this._visibility); if (this._token) this.initialize(); else this.renderLogin(); }
  disconnectedCallback() { clearInterval(this._poll); this._session.abort(); this._jobs.clear(); document.removeEventListener('visibilitychange', this._visibility); this.closeModal(); }
  get token() { return this._token; }
  set token(value) {
    this._token = typeof value === 'string' ? value.trim() : '';
    this._generation += 1;
    this._session.abort();
    this._session = new AbortController();
    clearInterval(this._poll);
    this._identity = null;
    this._selectedAccount = null;
    this._selectedConversation = null;
    this._accounts = [];
    this._messages = [];
    this._conversations = [];
    this._drafts.clear();
    this._pendingSends.clear();
    this._jobs.clear();
    this._connectionJobs.clear();
    this._filterAccountId = ''; this._filterChannel = ''; this._inboxQuery = ''; this._inboxOffset = 0; this._inboxTotal = 0; this._inboxHasMore = false; this._inboxRequest += 1; this._activeConversation = null;
    this._conversationMutations.clear(); this._lastRead.clear(); this._reading.clear(); this._historyJobs.clear(); clearTimeout(this._searchTimer); clearTimeout(this._readTimer);
    this.closeModal();
    if (this.isConnected) this._token ? this.initialize() : this.renderLogin();
  }
  get baseUrl() { return this._baseUrl; }
  set baseUrl(value) { this._baseUrl = String(value || '').replace(/\/+$/, ''); }
  get account() { return this._accounts.find(a => a.id === this._selectedAccount); }
  get conversation() { return this._activeConversation?.id === this._selectedConversation ? this._activeConversation : this._conversations.find(c => c.id === this._selectedConversation); }
  capability(channel) { return this._capabilities.find(c => c.channel === channel); }

  async api(path, options = {}) {
    const controller = new AbortController();
    const session = this._session;
    const onAbort = () => controller.abort();
    session.signal.addEventListener('abort', onAbort, { once: true });
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(`${this._baseUrl}${path}`, { ...options, signal: controller.signal, cache: 'no-store', credentials: 'omit', headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(this._token ? { Authorization: `Bearer ${this._token}` } : {}), ...options.headers } });
      const body = response.status === 204 ? null : await response.json().catch(() => null);
      if (!response.ok) {
        let detail = body?.detail ?? body?.error ?? `HTTP ${response.status}`;
        if (Array.isArray(detail)) detail = detail.map(v => `${(v.loc || []).slice(1).join('.')}: ${v.msg}`).join('\n');
        if (typeof detail !== 'string') detail = detail?.message || JSON.stringify(detail);
        const error = new Error(detail);
        error.status = response.status;
        if (response.status === 401 && this._identity) {
          this.token = '';
          this.dispatchEvent(new CustomEvent('omni-auth-required', { bubbles: true, composed: true }));
        }
        throw error;
      }
      return body;
    } catch (error) {
      if (session.signal.aborted) throw new Error('Сеанс завершён.');
      if (error.name === 'AbortError') throw new Error('Сервер не ответил за 20 секунд. Проверьте соединение.');
      if (error instanceof TypeError) throw new Error('Нет связи с сервером. Проверьте сеть и адрес API.');
      throw error;
    } finally { clearTimeout(timeout); session.signal.removeEventListener('abort', onAbort); }
  }
  post(path, body) { return this.api(path, { method: 'POST', body: JSON.stringify(body) }); }

  async initialize() {
    const generation = this._generation;
    this._root.replaceChildren(el('div', { class: 'app' }, [this.brandHeader(), emptyState('Подключаем рабочее пространство', 'Загружаем ваши каналы и диалоги…')]));
    try {
      const [identity, accounts, capabilities] = await Promise.all([this.api('/api/me'), this.api('/api/accounts'), this.api('/api/capabilities')]);
      if (generation !== this._generation) return;
      this._identity = identity;
      this._accounts = array(accounts);
      this._capabilities = array(capabilities);
      this.renderShell();
      await this.loadInbox();
      this._poll = setInterval(() => { if (!document.hidden) this.refresh(); }, 3000);
      this.dispatchEvent(new CustomEvent('omni-ready', { detail: { user: { sub: identity.sub, tenant_id: identity.tenant_id, role: identity.role } }, bubbles: true, composed: true }));
    } catch (error) { if (generation === this._generation) this.renderLogin(error.message); }
  }

  brandHeader() {
    return el('header', { class: 'topbar' }, [el('div', { class: 'brand' }, [el('span', { class: 'brand-mark' }, icon('chat', 21)), el('div', { class: 'brand-name' }, ['omni', el('span', {}, 'crm')])]), el('span', { class: 'topbar-divider' }), el('span', { class: 'topbar-title' }, 'Рабочие диалоги')]);
  }
  renderLogin(errorMessage = '', runtime = null) {
    if (!this.hasAttribute('standalone')) {
      this._root.replaceChildren(el('div', { class: 'app' }, emptyState('Войдите через вашу CRM', errorMessage || 'Рабочее пространство откроется после авторизации.')));
      return;
    }
    const localMode = runtime?.local_test === true;
    const demoMode = runtime?.demo === true;
    const error = el('div', { class: 'form-error', hidden: !errorMessage }, errorMessage);
    const entry = button(!runtime ? 'Проверяем режим сервера…' : localMode ? 'Открыть локальный стенд' : 'Открыть демо-пространство', async () => {
      entry.disabled = true; error.hidden = true;
      try { const response = await this.post(localMode ? '/api/local/token' : '/api/dev/token', {}); if (!response?.access_token) throw new Error('Сервер не вернул токен доступа.'); this.token = response.access_token; }
      catch (e) { error.textContent = e.status === 404 || e.status === 403 ? localMode ? 'Локальный вход недоступен. Откройте стенд на этом компьютере или используйте авторизацию CRM.' : 'Демо-режим отключён на этом сервере. Для входа используйте авторизацию вашей CRM.' : e.message; error.hidden = false; entry.disabled = false; }
    }, 'primary full', 'arrow');
    entry.disabled = !runtime;
    entry.hidden = !!runtime && !localMode && !demoMode;
    const token = el('textarea', { id: 'crm-token', name: 'token', rows: 3, placeholder: 'JWT от вашей CRM', autocomplete: 'off', spellcheck: 'false', required: true });
    const tokenForm = el('form', { onsubmit: event => { event.preventDefault(); const value = token.value; token.value = ''; this.token = value; } }, [el('div', { class: 'field' }, [el('label', { for: 'crm-token' }, 'Токен текущего сотрудника'), token, el('span', { class: 'field-help' }, 'Токен хранится только в памяти этой вкладки. В рабочей CRM вход выполняется автоматически.')]), el('button', { class: 'button full', type: 'submit' }, 'Войти в рабочее пространство')]);
    const copy = el('section', { class: 'login-copy' }, [el('p', { class: 'eyebrow' }, 'Единое пространство команды продаж'), el('h1', {}, ['Все диалоги.', el('br'), el('span', {}, 'Личный контакт.')]), el('p', {}, 'Общайтесь с клиентами в привычных им каналах. Рабочие аккаунты, сообщения и история — внутри вашей CRM.'), el('div', { class: 'login-channels', 'aria-label': 'Telegram, WhatsApp, MAX и SMS' }, ['telegram', 'whatsapp_web', 'max_personal', 'sms_gateway'].map(channelIcon)), el('p', { class: 'login-footnote' }, 'Доступные способы подключения зависят от настроек сервера. Неофициальные интеграции отмечены отдельно.')]);
    const description = !runtime ? 'Загружаем доступные способы входа.' : runtime.unavailable ? 'Не удалось определить режим сервера. Обновите страницу или войдите с токеном CRM.' : localMode ? 'Подключайте свои аккаунты. Реальные сообщения отправляются только по кнопке «Отправить».' : demoMode ? 'В демо можно проверить интерфейс и обмен тестовыми сообщениями. Внешние мессенджеры подключаются отдельно.' : 'Войдите через вашу CRM или используйте выданный ею токен доступа.';
    const card = el('section', { class: 'login-card' }, [el('h2', {}, localMode ? 'Локальный стенд' : 'Ваше рабочее пространство'), el('p', {}, description), error, entry.hidden ? null : entry, localMode ? el('div', { class: 'login-caption' }, 'Ваш компьютер · ваши рабочие аккаунты') : demoMode ? el('div', { class: 'login-caption' }, 'Только тестовый канал · без реальных отправок') : null, el('details', { ...(!localMode && !demoMode && runtime ? { open: '' } : {}) }, [el('summary', {}, 'Войти с токеном CRM'), tokenForm])]);
    this._root.replaceChildren(el('div', { class: 'login-page' }, [this.brandHeader(), el('main', { class: 'login-main' }, [copy, card])]));
    if (!runtime) {
      if (!this._runtimePromise || this._runtimeBaseUrl !== this._baseUrl) {
        this._runtimeBaseUrl = this._baseUrl;
        this._runtimePromise = this.api('/api/runtime').then(value => ({ local_test: value?.local_test === true, demo: value?.demo === true })).catch(error => error.status === 404 ? { local_test: false, demo: true } : { local_test: false, demo: false, unavailable: true });
      }
      this._runtimePromise.then(value => { if (card.isConnected && !this._identity) this.renderLogin(errorMessage, value); });
    }
  }

  renderShell() {
    const header = this.brandHeader();
    this._connectionLabel = el('span', {}, 'Обновление каждые 3 с');
    const identityName = this._identity.sub === 'local-owner' && this._identity.tenant_id === 'local-test' ? 'Локальный администратор' : this._identity.sub;
    header.append(el('div', { class: 'topbar-end' }, [el('div', { class: 'connection-pill' }, [el('span', { class: 'status-dot green' }), this._connectionLabel]), el('div', { class: 'identity' }, [el('div', { class: 'avatar' }, initials(identityName)), el('div', {}, [el('div', { class: 'identity-name' }, identityName), el('div', { class: 'identity-role' }, this._identity.role === 'admin' ? 'Администратор' : 'Менеджер')])]), iconButton('logout', 'Выйти', () => { this.token = ''; this.dispatchEvent(new CustomEvent('omni-logout', { bubbles: true, composed: true })); })]));
    this._errorBanner = el('div', { class: 'error-banner', role: 'alert', hidden: true });
    this._conversationCount = el('span', { class: 'count' }, '0');
    this._search = el('input', { type: 'search', placeholder: 'Имя, компания или телефон', 'aria-label': 'Найти диалог', oninput: () => { this._inboxQuery = this._search.value.trim(); this._inboxOffset = 0; this._inboxRequest += 1; clearTimeout(this._searchTimer); this._searchTimer = setTimeout(() => this.loadInbox(), 250); } });
    this._channelFilter = el('select', { 'aria-label': 'Мессенджер', onchange: () => { this._filterChannel = this._channelFilter.value; this._filterAccountId = ''; this._inboxOffset = 0; this.renderAccounts(); this.loadInbox(); } });
    this._accountFilter = el('select', { 'aria-label': 'Рабочий аккаунт', onchange: () => { this._filterAccountId = this._accountFilter.value; this._inboxOffset = 0; this.loadInbox(); } });
    this._historyButton = button('Загрузить историю', () => this.openHistoryImport(), '', 'refresh');
    this._inboxStatus = el('div', { class: 'inbox-status', role: 'status', 'aria-live': 'polite' });
    this._conversationList = el('div', { class: 'inbox-table-scroll' });
    this._conversationFooter = el('div', { class: 'inbox-pagination' });
    const filters = el('div', { class: 'inbox-filters' }, [el('div', { class: 'search inbox-search' }, [icon('search', 16), this._search]), el('label', { class: 'inbox-filter' }, [el('span', {}, 'Мессенджер'), this._channelFilter]), el('label', { class: 'inbox-filter account-filter' }, [el('span', {}, 'Рабочий аккаунт'), this._accountFilter]), iconButton('settings', 'Выбрать канал', () => this._accountFilter.focus(), 'inbox-filter-focus')]);
    const toolbar = el('div', { class: 'inbox-toolbar' }, [el('div', {}, [el('div', { class: 'inbox-heading' }, [el('h1', {}, 'Все диалоги'), this._conversationCount]), el('p', {}, 'Клиенты и переписка из всех рабочих мессенджеров')]), el('div', { class: 'inbox-actions' }, [this._historyButton, button('Подключить канал', () => this.openConnect(), '', 'plus'), button('Новый диалог', () => this.openNewConversation(), 'primary', 'plus')])]);
    this._inboxPanel = el('section', { class: 'inbox-panel', 'aria-label': 'Все диалоги' }, [toolbar, filters, this._inboxStatus, this._conversationList, this._conversationFooter]);
    this._chatPanel = el('section', { class: 'chat-panel', 'aria-label': 'Переписка', hidden: true });
    this._workspace = el('main', { class: 'workspace inbox-workspace' }, [this._inboxPanel, this._chatPanel]);
    this._root.replaceChildren(el('div', { class: 'app inbox-app' }, [header, this._errorBanner, this._workspace]));
    this._channelSignature = null; this._accountSignature = null;
    this.renderAccounts(); this.renderConversations(); this.renderChat();
  }
  showError(message) {
    if (!this._errorBanner?.isConnected) return;
    this._errorBanner.replaceChildren(el('span', {}, message), iconButton('close', 'Закрыть уведомление', () => { this._errorBanner.hidden = true; }));
    this._errorBanner.hidden = false;
  }
  renderAccounts() {
    if (!this._accountFilter) return;
    const channels = [...new Set(this._accounts.map(account => account.channel))];
    const channelSignature = JSON.stringify(channels);
    if (this._channelSignature !== channelSignature) { this._channelSignature = channelSignature; this._channelFilter.replaceChildren(el('option', { value: '' }, 'Все мессенджеры'), ...channels.map(channel => el('option', { value: channel }, CHANNEL_LABELS[channel] || channel))); }
    this._channelFilter.value = this._filterChannel || '';
    const accounts = this._accounts.filter(account => !this._filterChannel || account.channel === this._filterChannel);
    const signature = JSON.stringify(accounts.map(account => [account.id, account.label, account.state]));
    if (signature !== this._accountSignature) { this._accountSignature = signature; this._accountFilter.replaceChildren(el('option', { value: '' }, 'Все аккаунты'), ...accounts.map(account => el('option', { value: account.id }, `${account.label} · ${CHANNEL_LABELS[account.channel] || account.channel}`))); }
    this._accountFilter.value = this._filterAccountId || '';
    this._historyButton.disabled = !this._accounts.some(account => ['telegram', 'whatsapp_web', 'max_personal'].includes(account.channel) && account.state === 'connected');
  }
  async selectAccount(id) {
    this.closeChat();
    this._selectedAccount = id;
    this._filterAccountId = id || '';
    this._filterChannel = '';
    this._inboxQuery = ''; this._search.value = ''; this._inboxOffset = 0;
    this.renderAccounts();
    await this.loadInbox();
  }
  inboxPath() {
    const params = new URLSearchParams({ limit: String(this._inboxPageSize || 50), offset: String(this._inboxOffset || 0) });
    if (this._inboxQuery) params.set('q', this._inboxQuery);
    if (this._filterAccountId) params.set('account_id', this._filterAccountId);
    if (this._filterChannel) params.set('channel', this._filterChannel);
    return `/api/inbox?${params}`;
  }
  async loadInbox(quiet = false) {
    const request = ++this._inboxRequest, generation = this._generation, path = this.inboxPath();
    if (!quiet) { this._inboxLoading = true; this._inboxStatus.textContent = 'Загружаем диалоги…'; }
    try {
      const result = await this.api(path);
      if (request !== this._inboxRequest || generation !== this._generation) return;
      this._conversations = array(result); this._inboxTotal = Number(result.total ?? this._conversations.length); this._inboxHasMore = result.has_more === true;
      const active = this._conversations.find(item => item.id === this._selectedConversation);
      if (active) this._activeConversation = active;
      this._inboxLoading = false; this._inboxStatus.textContent = ''; this.renderConversations();
    } catch (error) { if (request === this._inboxRequest && generation === this._generation) { this._inboxLoading = false; this._inboxStatus.replaceChildren(el('span', {}, error.message), button('Повторить загрузку', () => this.loadInbox(), 'subtle', 'refresh')); } }
  }
  updateConversation(item) {
    this._conversations = this._conversations.map(current => current.id === item.id ? { ...current, ...item } : current);
    if (this._selectedConversation === item.id) this._activeConversation = { ...this._activeConversation, ...item };
  }
  async patchConversation(conversation, patch, control) {
    if (this._conversationMutations.has(conversation.id)) return;
    this._conversationMutations.add(conversation.id); if (control) control.disabled = true;
    try { const updated = await this.api(`/api/conversations/${encodeURIComponent(conversation.id)}`, { method: 'PATCH', body: JSON.stringify(patch) }); this.updateConversation(updated); }
    catch (error) { this.showError(error.message); }
    finally { this._conversationMutations.delete(conversation.id); this.renderConversations(); if (control?.isConnected) control.disabled = false; }
  }
  renderConversations() {
    if (!this._conversationList) return;
    this._conversationCount.textContent = String(this._inboxTotal || 0);
    const head = el('thead', {}, el('tr', {}, [el('th', { scope: 'col', class: 'favorite-col' }, el('span', { class: 'sr-only' }, 'Избранное')), el('th', { scope: 'col' }, 'Клиент'), el('th', { scope: 'col', class: 'channel-col' }, 'Мессенджер'), el('th', { scope: 'col', class: 'activity-col' }, 'Время'), el('th', { scope: 'col', class: 'waiting-col' }, 'Ожидает ответа')]));
    const body = el('tbody');
    for (const conversation of this._conversations) {
      const account = this._accounts.find(item => item.id === conversation.account_id);
      const channel = conversation.channel || account?.channel || '';
      const name = conversation.title || conversation.peer;
      const favorite = iconButton('star', `${conversation.favorite ? 'Убрать из избранного' : 'В избранное'}: ${name}`, event => { event.stopPropagation(); this.patchConversation(conversation, { favorite: !conversation.favorite }, favorite); }, `favorite-button${conversation.favorite ? ' active' : ''}`);
      favorite.setAttribute('aria-pressed', String(!!conversation.favorite));
      favorite.disabled = this._conversationMutations.has(conversation.id);
      const timestamp = conversation.last_activity_at || conversation.last_message_at;
      const unread = Number(conversation.unread_count || 0);
      const row = el('tr', { class: `inbox-row${conversation.id === this._selectedConversation ? ' selected' : ''}${unread ? ' unread' : ''}`, 'data-conversation-id': conversation.id, tabindex: '0', 'aria-label': `${name}, ${CHANNEL_LABELS[channel] || channel}, ${conversation.account_label || account?.label || ''}`, onclick: () => this.selectConversation(conversation.id), onkeydown: event => { if (event.target !== event.currentTarget) return; if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.selectConversation(conversation.id); } } }, [el('td', { class: 'favorite-col' }, favorite), el('td', { class: 'customer-cell' }, el('div', { class: 'customer-content' }, [el('div', { class: 'avatar' }, initials(name)), el('div', { class: 'customer-copy' }, [el('div', { class: 'customer-name' }, [el('span', {}, name), unread ? el('span', { class: 'unread-badge', 'aria-label': `Непрочитанных: ${unread}` }, unread > 99 ? '99+' : unread) : null]), el('p', { class: 'customer-preview' }, [channelIcon(channel), el('span', {}, conversation.preview || conversation.last_message?.text || 'В этом диалоге пока нет сообщений')])])])), el('td', { class: 'channel-col' }, el('div', { class: 'table-channel' }, [channelIcon(channel), el('div', {}, [el('strong', {}, CHANNEL_LABELS[channel] || channel), el('span', {}, conversation.account_label || account?.label || '')])])), el('td', { class: 'activity-col' }, timestamp ? [el('time', { datetime: timestamp, title: dateLabel(timestamp) }, timeLabel(timestamp)), el('span', { class: 'table-date' }, shortDateLabel(timestamp))] : '—'), el('td', { class: `waiting-col${conversation.waiting_since ? ' has-wait' : ''}`, title: conversation.waiting_since ? `С ${dateLabel(conversation.waiting_since)} ${timeLabel(conversation.waiting_since)}` : '' }, conversation.waiting_since ? [icon('clock', 13), waitingLabel(conversation.waiting_since)] : '—')]);
      body.append(row);
    }
    this._conversationList.replaceChildren(el('table', { class: 'inbox-table', 'aria-label': 'Диалоги из всех мессенджеров' }, [head, body]));
    if (!this._conversations.length) this._conversationList.append(emptyState(this._inboxQuery ? 'Диалоги не найдены' : 'Пока нет диалогов', this._inboxQuery ? 'Попробуйте другое имя, компанию или номер.' : 'Подключите аккаунт, загрузите доступную историю или начните новый диалог.', button(this._accounts.length ? 'Новый диалог' : 'Подключить канал', () => this._accounts.length ? this.openNewConversation() : this.openConnect(), '', 'plus'), true));
    const offset = this._inboxOffset || 0, count = this._conversations.length;
    const prev = button('Назад', () => { this._inboxOffset = Math.max(0, offset - this._inboxPageSize); this.loadInbox(); }, 'subtle', 'arrow'); prev.disabled = offset === 0;
    const next = button('Далее', () => { this._inboxOffset = offset + this._inboxPageSize; this.loadInbox(); }, 'subtle'); next.disabled = !this._inboxHasMore;
    this._conversationFooter.replaceChildren(el('span', {}, count ? `${offset + 1}–${offset + count} из ${this._inboxTotal} диалогов` : '0 диалогов'), el('div', { class: 'pagination-actions' }, [prev, next]));
  }
  closeChat() {
    if (this._composer && this._selectedConversation) this._drafts.set(this._selectedConversation, this._composer.value);
    this._selectedConversation = null; this._activeConversation = null; this._messages = []; this._hasOlder = false;
    this._workspace?.classList.remove('chat-open'); if (this._chatPanel) this._chatPanel.hidden = true;
    this.renderConversations();
  }
  async selectConversation(id, supplied = null) {
    const item = supplied || this._conversations.find(conversation => conversation.id === id) || (this._activeConversation?.id === id ? this._activeConversation : null);
    if (!item) return;
    if (this._composer && this._selectedConversation) this._drafts.set(this._selectedConversation, this._composer.value);
    this._selectedConversation = id; this._activeConversation = item; this._selectedAccount = item.account_id;
    this._messages = []; this._hasOlder = false; this._messagesLoading = true;
    const generation = this._generation;
    this._workspace.classList.add('chat-open'); this._chatPanel.hidden = false;
    this.renderConversations(); this.renderChat();
    try {
      const result = array(await this.api(`/api/conversations/${encodeURIComponent(id)}/messages?limit=100`));
      if (this._selectedConversation !== id || generation !== this._generation) return;
      this._messages = result; this._hasOlder = result.length >= 100; this._messagesLoading = false; this.renderMessages(true);
    } catch (error) { if (this._selectedConversation === id && generation === this._generation) { this._messagesLoading = false; this.renderMessages(); this.showError(error.message); } }
  }
  async markVisibleMessagesRead() {
    const id = this._selectedConversation, list = this._messageList, generation = this._generation;
    if (!id || !list?.isConnected || document.hidden || this._modal || !this._workspace.classList.contains('chat-open') || this._reading.has(id)) return;
    const bounds = list.getBoundingClientRect(); if (!bounds.height || !bounds.width) return;
    const visible = [...list.querySelectorAll('[data-message-id][data-message-direction="inbound"][data-read-eligible="true"]')].filter(node => { const box = node.getBoundingClientRect(); return box.bottom > bounds.top && box.top < bounds.bottom; }).map(node => Number(node.dataset.messageId)).filter(Number.isSafeInteger);
    const lastId = Math.max(0, ...visible); if (!lastId || lastId <= (this._lastRead.get(id) || 0)) return;
    this._reading.add(id);
    try {
      const result = await this.post(`/api/conversations/${encodeURIComponent(id)}/read`, { last_message_id: lastId });
      if (generation !== this._generation) return;
      this._lastRead.set(id, Math.max(this._lastRead.get(id) || 0, Number(result.last_read_message_id || lastId)));
      this.updateConversation({ id, unread_count: result.unread_count }); this.renderConversations();
    } catch { /* Keep unread counts unchanged when the acknowledgement is unavailable. */ }
    finally { this._reading.delete(id); }
  }
  renderChat() {
    this._composer = null;
    this._templateEnabled = false;
    this._templateControls = null;
    if (!this.conversation) {
      this._chatPanel.replaceChildren(emptyState(this.account ? 'Продолжите разговор' : 'Первое сообщение — начало отношений', this.account ? 'Выберите диалог слева или создайте новый. Здесь будет вся история общения с клиентом.' : 'Подключите рабочий канал, чтобы общаться с клиентами из одного пространства.', this.account ? button('Новый диалог', () => this.openNewConversation(), 'primary', 'plus') : button('Подключить канал', () => this.openConnect(), 'primary', 'plus')));
      return;
    }
    const conversation = this.conversation;
    const account = this.account;
    if (!account) { this.closeChat(); return; }
    const capability = this.capability(account.channel);
    this._chatHeaderState = el('span', { class: `status-dot ${stateColor(account.state)}` });
    const header = el('header', { class: 'chat-header' }, [iconButton('arrow', 'К списку диалогов', () => this.closeChat(), 'chat-back'), el('div', { class: 'avatar large' }, initials(conversation.title || conversation.peer)), el('div', { class: 'chat-heading' }, [el('h2', {}, conversation.title || conversation.peer), el('div', { class: 'chat-subtitle' }, [this._chatHeaderState, el('span', {}, `${CHANNEL_LABELS[account.channel] || account.channel} · ${conversation.peer}`)])]), modeBadge(capability, account.channel), iconButton('edit', 'Переименовать клиента', () => this.openRenameConversation()), iconButton('settings', 'Настроить аккаунт', () => this.openManageAccount(this.account))]);
    let notice = '';
    if (account.channel === 'demo') notice = 'Тестовый канал: сообщения не отправляются во внешние мессенджеры.';
    else if (account.channel === 'whatsapp_cloud') notice = 'Свободный текст доступен в течение 24 часов после сообщения клиента. Вне этого окна выберите одобренный шаблон.';
    else if (account.channel === 'max_bot') notice = 'Сообщения отправляются от бота компании. Клиент должен сначала начать диалог с ботом.';
    else if (capability?.mode && /experimental|unofficial/.test(capability.mode)) notice = 'Экспериментальная интеграция. Отслеживайте состояние подключения и подтверждение отправки.';
    this._messageList = el('div', { class: 'messages', role: 'log', 'aria-label': 'История сообщений', 'aria-live': 'polite', 'aria-relevant': 'additions', onscroll: () => { clearTimeout(this._readTimer); this._readTimer = setTimeout(() => this.markVisibleMessagesRead(), 150); } });
    this._composer = el('textarea', { class: 'composer', rows: 2, placeholder: 'Напишите сообщение…', 'aria-label': 'Текст сообщения', oninput: () => { this._drafts.set(conversation.id, this._composer.value); this.updateSendButton(); }, onkeydown: event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); this.sendMessage(); } } });
    this._composer.value = this._drafts.get(conversation.id) || '';
    this._sendButton = button('Отправить', () => this.sendMessage(), 'primary', 'send');
    this._sendNotice = el('div', { class: 'form-error', hidden: true, role: 'alert' });
    const footer = el('div', { class: 'composer-footer' }, [el('span', {}, `От: ${account.label}`), account.channel === 'demo' ? el('button', { type: 'button', onclick: () => this.openDemoInbound() }, 'Добавить тестовый ответ') : el('span', {}, 'История сохраняется в CRM')]);
    const templateOptions = account.channel === 'whatsapp_cloud' ? this.renderTemplateOptions(conversation.id) : null;
    const composer = el('div', { class: 'composer-area' }, [this._sendNotice, templateOptions, el('div', { class: 'composer-box' }, [this._composer, el('div', { class: 'composer-tools' }, [el('span', { class: 'composer-hint' }, 'Ctrl / ⌘ + Enter — отправить'), this._sendButton])]), footer]);
    this._chatPanel.replaceChildren(header, el('div', { class: 'chat-notice' }, notice ? [icon('lock', 12), notice] : []), this._messageList, composer);
    this.updateSendButton(); this.renderMessages(true);
  }
  renderTemplateOptions(conversationId) {
    const pending = this._pendingSends.get(conversationId);
    const templateName = this.field('template-name', 'Имя одобренного шаблона', 'text', false, 'Например, offer_followup');
    const language = this.field('template-language', 'Код языка', 'text', false, 'ru');
    const parameters = el('textarea', { id: 'template-parameters', rows: 2, placeholder: 'Каждый параметр body — с новой строки', 'aria-label': 'Параметры шаблона', oninput: () => this.updateSendButton() });
    this._templateName = templateName.input;
    this._templateLanguage = language.input;
    this._templateParameters = parameters;
    this._templateName.value = pending?.template?.name || '';
    this._templateLanguage.value = pending?.template?.language || 'ru';
    parameters.value = pending?.template?.parameters?.join('\n') || '';
    this._templateName.addEventListener('input', () => this.updateSendButton());
    this._templateLanguage.addEventListener('input', () => this.updateSendButton());
    const fields = el('div', { class: 'template-fields', hidden: true }, [el('div', { class: 'template-row' }, [templateName.node, language.node]), el('div', { class: 'field' }, [el('label', { for: 'template-parameters' }, 'Подстановки в текст шаблона'), parameters]), el('p', { class: 'field-help' }, 'Только одобренный шаблон с текстовыми параметрами body. Порядок строк соответствует {{1}}, {{2}} и далее. Изображения, header и кнопки здесь не настраиваются.')]);
    const toggle = button('Одобренный шаблон', () => {
      this._templateEnabled = !this._templateEnabled;
      fields.hidden = !this._templateEnabled;
      toggle.setAttribute('aria-pressed', String(this._templateEnabled));
      toggle.classList.toggle('selected', this._templateEnabled);
      this._composer.placeholder = this._templateEnabled ? 'Описание для истории CRM (необязательно). Клиент получит текст одобренного шаблона.' : 'Напишите сообщение…';
      this.updateSendButton();
    }, 'template-toggle');
    toggle.setAttribute('aria-pressed', 'false');
    this._templateControls = [toggle, this._templateName, this._templateLanguage, parameters];
    if (pending?.template) { this._templateEnabled = true; fields.hidden = false; toggle.setAttribute('aria-pressed', 'true'); toggle.classList.add('selected'); }
    return el('div', { class: 'template-options' }, [toggle, fields]);
  }
  renderMessages(scrollToBottom = false) {
    if (!this._messageList?.isConnected) return;
    const list = this._messageList;
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 95;
    const oldTop = list.scrollTop;
    const nodes = [];
    if (this._hasOlder) nodes.push(el('div', { class: 'load-older' }, button('Загрузить предыдущие сообщения', () => this.loadOlder(), 'subtle', 'refresh')));
    let previousDay = '';
    for (const message of this._messages) {
      const day = dateLabel(message.created_at);
      if (day && day !== previousDay) { nodes.push(el('div', { class: 'day-divider' }, el('span', {}, day))); previousDay = day; }
      const outbound = ['outbound', 'out', 'outgoing'].includes(message.direction);
      const status = MESSAGE_STATUSES[message.status] ?? message.status;
      const content = el('div', { class: 'message' }, [el('div', { class: 'message-text' }, message.text || '[Сообщение без текста]'), el('div', { class: `message-meta ${message.status}` }, [el('time', { datetime: message.created_at || '' }, timeLabel(message.created_at)), outbound ? el('span', {}, status) : null, outbound && ['sent', 'delivered', 'read'].includes(message.status) ? icon(message.status === 'sent' ? 'check' : 'checks', 12) : null]), message.error ? el('div', { class: 'message-error' }, typeof message.error === 'string' ? message.error : JSON.stringify(message.error)) : null, outbound && message.status === 'uncertain' ? el('div', { class: 'message-error' }, 'Проверьте чат в мессенджере перед повторной отправкой.') : null]);
      nodes.push(el('div', { class: `message-row${outbound ? ' outbound' : ''}`, 'data-message-id': message.id, 'data-message-direction': outbound ? 'outbound' : 'inbound', 'data-read-eligible': !outbound && message.read_eligible === true ? 'true' : 'false' }, content));
    }
    if (!nodes.length) nodes.push(emptyState(this._messagesLoading ? 'Загружаем историю' : 'Начните разговор', this._messagesLoading ? 'Получаем сохранённые сообщения…' : 'Сообщения этого диалога появятся здесь.', null, true));
    list.replaceChildren(...nodes);
    if (scrollToBottom || nearBottom) list.scrollTop = list.scrollHeight;
    else list.scrollTop = oldTop;
    requestAnimationFrame(() => this.markVisibleMessagesRead());
  }
  async loadOlder() {
    if (this._loadingOlder || !this._messages.length) return;
    const id = this._selectedConversation;
    const first = this._messages[0].id;
    this._loadingOlder = true;
    try {
      const older = array(await this.api(`/api/conversations/${encodeURIComponent(id)}/messages?before=${encodeURIComponent(first)}&limit=100`));
      if (id !== this._selectedConversation) return;
      const oldHeight = this._messageList.scrollHeight, oldTop = this._messageList.scrollTop;
      const ids = new Set(this._messages.map(m => m.id));
      this._messages = [...older.filter(m => !ids.has(m.id)), ...this._messages];
      this._hasOlder = older.length >= 100;
      this.renderMessages();
      this._messageList.scrollTop = oldTop + this._messageList.scrollHeight - oldHeight;
    } catch (error) { this.showError(error.message); }
    finally { this._loadingOlder = false; }
  }
  updateSendButton() {
    if (!this._sendButton || !this._composer) return;
    const pending = this._pendingSends.get(this._selectedConversation);
    const retry = pending?.retryable;
    const validTemplate = this._templateEnabled && this._templateName?.value.trim() && this._templateLanguage?.value.trim();
    this._sendButton.disabled = pending?.busy || (!retry && (this._templateEnabled ? !validTemplate : !this._composer.value.trim()));
    this._sendButton.replaceChildren(icon(retry ? 'refresh' : 'send', 14), pending?.busy ? 'Отправляем…' : retry ? 'Повторить запрос' : 'Отправить');
    this._composer.disabled = !!pending;
    this._templateControls?.forEach(control => { control.disabled = !!pending; });
    if (this._sendNotice) {
      this._sendNotice.hidden = !pending?.error;
      this._sendNotice.textContent = pending?.error || '';
    }
  }
  async sendMessage() {
    const conversationId = this._selectedConversation;
    if (!conversationId || !this._composer) return;
    let pending = this._pendingSends.get(conversationId);
    if (pending?.busy) return;
    let template = pending?.template;
    if (!pending && this._templateEnabled) {
      if (!this._templateName.value.trim() || !this._templateLanguage.value.trim()) return;
      template = { name: this._templateName.value.trim(), language: this._templateLanguage.value.trim(), parameters: this._templateParameters.value.trim() ? this._templateParameters.value.split('\n').map(value => value.trim()) : [] };
    }
    const text = pending?.text ?? (this._composer.value.trim() || (template ? `[Шаблон: ${template.name}]` : ''));
    if (!text) return;
    pending ||= { text, client_id: uuid(), ...(template ? { template } : {}) };
    pending.busy = true; pending.error = ''; pending.retryable = false;
    this._pendingSends.set(conversationId, pending);
    this.updateSendButton();
    try {
      const message = await this.post(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, { text: pending.text, client_id: pending.client_id, ...(pending.template ? { template: pending.template } : {}) });
      this._pendingSends.delete(conversationId); this._drafts.delete(conversationId);
      if (conversationId === this._selectedConversation) {
        this._composer.value = '';
        this._messages = [...this._messages.filter(m => m.id !== message.id), message];
        this.renderMessages(true);
      }
      this.refresh();
    } catch (error) {
      pending.busy = false;
      if (error.status && error.status < 500 && ![408, 429].includes(error.status)) {
        this._pendingSends.delete(conversationId);
        this.showError(error.message);
      } else {
        pending.retryable = true;
        pending.error = `${error.message} Результат запроса пока неизвестен. Повтор использует тот же идентификатор и не создаёт новое сообщение.`;
      }
    } finally { if (conversationId === this._selectedConversation) this.updateSendButton(); }
  }
  async refresh() {
    if (this._refreshing || !this._identity || !this.isConnected) return;
    this._refreshing = true;
    const conversationId = this._selectedConversation, generation = this._generation;
    try {
      const requests = [this.api('/api/accounts'), this.loadInbox(true)];
      if (conversationId) requests.push(this.api(`/api/conversations/${encodeURIComponent(conversationId)}/messages?limit=100`));
      const results = await Promise.all(requests);
      if (generation !== this._generation) return;
      this._accounts = array(results[0]); this.renderAccounts(); this._modalRefresh?.();
      if (conversationId === this._selectedConversation && conversationId) {
        const fresh = array(results[2]);
        const freshIds = new Set(fresh.map(message => message.id));
        const merged = [...this._messages.filter(message => !freshIds.has(message.id)), ...fresh].sort((a, b) => (date(a.created_at)?.getTime() || 0) - (date(b.created_at)?.getTime() || 0) || Number(a.id) - Number(b.id));
        if (JSON.stringify(merged) !== JSON.stringify(this._messages)) { this._messages = merged; this.renderMessages(); }
        else this.markVisibleMessagesRead();
        if (this._chatHeaderState && this.account) this._chatHeaderState.className = `status-dot ${stateColor(this.account.state)}`;
      }
      this._connectionLabel.textContent = 'Обновлено ' + timeLabel(new Date().toISOString());
    } catch (error) { if (generation === this._generation) { this._connectionLabel.textContent = 'Нет обновлений'; this.showError(error.message); } }
    finally { this._refreshing = false; }
  }

  openModal(title, description = '') {
    this.closeModal();
    const previousFocus = this.shadowRoot.activeElement;
    const content = el('div');
    const modal = el('section', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'modal-title', tabindex: '-1' }, [el('div', { class: 'modal-header' }, [el('div', {}, [el('h2', { id: 'modal-title' }, title), description ? el('p', {}, description) : null]), iconButton('close', 'Закрыть окно', () => this.closeModal())]), content]);
    this._modal = el('div', { class: 'modal-backdrop', onclick: event => { if (event.target === this._modal) this.closeModal(); }, onkeydown: event => {
      if (event.key === 'Escape') this.closeModal();
      if (event.key !== 'Tab') return;
      const focusable = Array.from(modal.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')).filter(n => !n.hidden && n.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1], active = this.shadowRoot.activeElement;
      if (event.shiftKey && (active === first || active === modal)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && active === last) { event.preventDefault(); first?.focus(); }
    } }, modal);
    this._modalPreviousFocus = previousFocus;
    this._root.append(this._modal);
    queueMicrotask(() => modal.focus());
    return content;
  }
  closeModal() { this._modalCleanup?.(); this._modalCleanup = null; this._modal?.remove(); this._modal = null; this._modalRefresh = null; this._modalPreviousFocus?.focus(); this._modalPreviousFocus = null; }
  field(name, label, type = 'text', required = false, placeholder = '') {
    const input = el('input', { id: `field-${name}`, name, type: ['password', 'number', 'tel', 'url', 'email'].includes(type) ? type : 'text', required, placeholder, autocomplete: 'off', spellcheck: 'false' });
    return { input, node: el('div', { class: 'field' }, [el('label', { for: `field-${name}` }, label + (required ? ' *' : '')), input]) };
  }
  formError() { return el('div', { class: 'form-error', role: 'alert', hidden: true }); }
  openConnect() {
    const content = this.openModal('Каналы связи', 'Подключите рабочий аккаунт и общайтесь с клиентами от своего имени.');
    this._modal.querySelector('.modal').classList.add('channels-modal');
    const connected = this._accounts.filter(account => ['telegram', 'whatsapp_web', 'max_personal'].includes(account.channel));
    if (connected.length) {
      content.append(el('h3', { class: 'channels-section-title' }, 'Ваши подключения'), el('div', { class: 'connected-channels' }, connected.map(account => el('button', { type: 'button', class: 'connected-channel', onclick: () => this.openManageAccount(account) }, [channelIcon(account.channel), el('div', { class: 'connected-channel-copy' }, [el('strong', {}, account.public?.identity?.phone || account.label), el('span', {}, CHANNEL_LABELS[account.channel])]), el('span', { class: `channel-state ${stateColor(account.state)}` }, [el('span', { class: `status-dot ${stateColor(account.state)}` }), stateLabel(account.state)])]))));
    }
    content.append(el('h3', { class: 'channels-section-title' }, connected.length ? 'Подключить ещё' : 'Выберите мессенджер'));
    const grid = el('div', { class: 'provider-grid connection-grid' });
    const order = ['whatsapp_web', 'telegram', 'max_personal', 'whatsapp_cloud', 'max_bot', 'sms_gateway', 'demo'];
    const descriptions = { whatsapp_web: 'Ваш номер · вход по QR-коду', telegram: 'Ваш аккаунт · номер телефона или QR', max_personal: 'Ваш аккаунт · QR-код или номер телефона', whatsapp_cloud: 'Бизнес-номер компании', max_bot: 'Сообщения от бота', sms_gateway: 'Сообщения с рабочей SIM', demo: 'Проверка без реальных сообщений' };
    for (const capability of [...this._capabilities].sort((a, b) => order.indexOf(a.channel) - order.indexOf(b.channel))) {
      if (capability.channel === 'demo' && !capability.enabled) continue;
      const guided = ['telegram', 'whatsapp_web', 'max_personal'].includes(capability.channel);
      const configured = capability.enabled && capability.connection?.configured !== false;
      const methods = capability.connection?.methods || [];
      const description = ['telegram', 'max_personal'].includes(capability.channel) && !methods.includes('qr') ? 'Ваш аккаунт · номер телефона и код' : capability.channel === 'max_personal' && !methods.some(method => ['phone', 'phone_code'].includes(method)) ? 'Ваш аккаунт · вход по QR-коду' : descriptions[capability.channel];
      grid.append(el('button', { type: 'button', class: `provider-option channel-tile${configured ? '' : ' needs-setup'}`, onclick: () => guided ? this.openGuidedConnection(capability.channel) : this.openAdvancedConnection(capability), 'aria-label': `Подключить ${CHANNEL_LABELS[capability.channel] || capability.label}` }, [channelIcon(capability.channel), el('span', { class: 'provider-name' }, CHANNEL_LABELS[capability.channel] || capability.label), el('span', { class: 'channel-description' }, description), el('span', { class: `channel-tile-action${configured ? '' : ' muted'}` }, configured ? 'Подключить' : 'Требуется настройка')]));
    }
    content.append(grid, el('p', { class: 'connection-footer' }, [icon('lock', 14), 'Доступ к каналам и истории определяется вашей учётной записью CRM.']));
    if (!this._capabilities.length) content.append(emptyState('Нет доступных интеграций', 'Обновите страницу или обратитесь к администратору.', null, true));
    if (this._identity.role === 'admin') content.append(button('Настройки Telegram для команды', () => this.openTelegramSettings(), 'subtle setup-link', 'settings'));
  }
  openAdvancedConnection(capability) {
    const content = this.openModal(CHANNEL_LABELS[capability.channel] || capability.label, 'Подключение рабочего канала');
    content.append(el('p', { class: 'provider-description' }, capability.description || ''));
    if (!capability.enabled) { content.append(el('div', { class: 'form-error' }, 'Подключение недоступно в текущей конфигурации сервера.'), button('Все каналы', () => this.openConnect(), '', 'arrow')); return; }
    const modal = this._modal, generation = this._generation;
    const label = this.field('label', 'Название аккаунта', 'text', true, 'Например, Анна · отдел продаж');
    const fields = (capability.config_fields || []).map(configField => ({ ...this.field(configField.name, configField.label || configField.name, configField.type, configField.required, configField.placeholder || ''), descriptor: configField }));
    const error = this.formError();
    const submit = el('button', { class: 'button primary', type: 'submit' }, 'Добавить и подключить');
    content.append(el('form', { onsubmit: async event => {
      event.preventDefault(); if (submit.disabled) return; submit.disabled = true; error.hidden = true;
      try {
        const config = {};
        for (const field of fields) { const value = field.input.value; if (value !== '') config[field.descriptor.name] = field.descriptor.type === 'number' ? Number(value) : value; }
        const account = await this.post('/api/accounts', { channel: capability.channel, label: label.input.value.trim(), config });
        for (const field of fields) field.input.value = '';
        if (modal !== this._modal || generation !== this._generation) return;
        this._accounts.push(account); this.renderAccounts();
        await this.selectAccount(account.id);
        if (modal === this._modal && generation === this._generation) this.openManageAccount(account, true);
      } catch (e) { if (modal === this._modal) { error.textContent = e.message; error.hidden = false; submit.disabled = false; } }
    } }, [label.node, ...fields.map(f => f.node), error, el('div', { class: 'form-actions' }, [button('Все каналы', () => this.openConnect()), submit])]));
  }
  openTelegramSettings() {
    if (this._identity?.role !== 'admin') return;
    const content = this.openModal('Telegram для команды', 'Однократная настройка администратором. Менеджеры входят по номеру телефона или QR-коду.');
    const modal = this._modal;
    const apiId = this.field('telegram-api-id', 'Telegram API ID', 'number', true);
    apiId.input.min = '1'; apiId.input.step = '1';
    const apiHash = this.field('telegram-api-hash', 'Telegram API hash', 'password', true);
    apiHash.input.pattern = '[a-fA-F0-9]{32}'; apiHash.input.minLength = 32; apiHash.input.maxLength = 32;
    const status = el('p', { class: 'provider-description', role: 'status' }, 'Проверяем настройки…');
    const error = this.formError();
    const submit = el('button', { type: 'submit', class: 'button primary' }, 'Сохранить и продолжить');
    content.append(status, el('p', { class: 'setup-instructions' }, ['Получите реквизиты приложения в ', el('a', { href: 'https://my.telegram.org/apps', target: '_blank', rel: 'noopener noreferrer' }, 'Telegram API development tools'), '. Они сохраняются на сервере в зашифрованном виде.']), el('form', { onsubmit: async event => {
      event.preventDefault(); if (submit.disabled) return; submit.disabled = true; error.hidden = true;
      try {
        await this.api('/api/integrations/telegram', { method: 'PUT', body: JSON.stringify({ api_id: Number(apiId.input.value), api_hash: apiHash.input.value.trim() }) });
        apiId.input.value = ''; apiHash.input.value = '';
        const capabilities = array(await this.api('/api/capabilities'));
        if (modal !== this._modal) return;
        this._capabilities = capabilities;
        this.openGuidedConnection('telegram');
      } catch (e) { if (modal === this._modal) { error.textContent = connectionError(e); error.hidden = false; submit.disabled = false; } }
    } }, [apiId.node, apiHash.node, error, el('div', { class: 'form-actions' }, [button('Все каналы', () => this.openConnect()), submit])]));
    this.api('/api/integrations/telegram').then(result => { if (modal === this._modal) status.textContent = result.configured ? 'Telegram настроен. Сохранённые реквизиты не отображаются. При необходимости можно заменить их.' : 'Укажите реквизиты одного приложения Telegram для подключения аккаунтов команды.'; }).catch(e => { if (modal === this._modal) { status.hidden = true; error.textContent = connectionError(e); error.hidden = false; } });
    this._modalCleanup = () => { apiId.input.value = ''; apiHash.input.value = ''; };
  }
  openGuidedConnection(channel, originalAccount = null, autoLogin = false) {
    const telegram = channel === 'telegram', max = channel === 'max_personal', whatsapp = channel === 'whatsapp_web';
    const channelName = CHANNEL_LABELS[channel];
    const capability = this.capability(channel);
    const supportsQr = capability?.connection?.methods?.includes('qr');
    const supportsPhone = telegram || capability?.connection?.methods?.some(method => ['phone', 'phone_code'].includes(method));
    const phoneFirst = telegram || (max && !supportsQr && supportsPhone);
    const content = this.openModal(`Подключение ${channelName}`);
    const modal = this._modal, generation = this._generation;
    const alive = () => modal === this._modal && generation === this._generation && this.isConnected;
    modal.querySelector('.modal').classList.add('connection-modal', telegram ? 'telegram-connection' : max ? 'max-connection' : 'whatsapp-connection');
    modal.querySelector('.modal-header').prepend(channelIcon(channel));
    const unavailable = !capability?.enabled || capability.connection?.configured === false;
    if (unavailable && !originalAccount) {
      content.append(el('div', { class: 'connection-unavailable' }, [el('div', { class: 'setup-illustration' }, icon('settings', 30)), el('h3', {}, 'Нужна настройка подключения'), el('p', {}, telegram ? 'Администратор должен один раз настроить Telegram для команды. После этого вы сможете войти по номеру телефона или QR-коду.' : `Администратор должен включить подключение ${channelName} на сервере.`), el('div', { class: 'form-actions' }, [button('Все каналы', () => this.openConnect()), telegram && this._identity.role === 'admin' ? button('Настроить Telegram', () => this.openTelegramSettings(), 'primary') : null])]));
      return;
    }
    let account = originalAccount, phase = phoneFirst ? 'phone' : 'qr', busy = false, lastSignature = '', inputPhone = originalAccount?.public?.phone || '', authMethod = originalAccount?.public?.auth_method || (phoneFirst ? 'phone' : 'qr');
    let showSuccess = false, lastError = '', lastErrorSource = '', disconnected = false, uncertainty = false, phoneEntry = phoneFirst && !originalAccount;
    let waQrWaitStartedAt = whatsapp ? Date.now() : null, waQrTimedOut = false;
    const waWaitMessage = 'Сервер не вернул QR-код за 2 минуты. Повторите подключение. Если ожидание повторится, попросите администратора проверить связь сервера с WhatsApp.';
    const stage = el('div', { class: 'connection-stage' });
    const error = this.formError();
    const jobStatus = el('div', { class: 'connection-progress', role: 'status', 'aria-live': 'polite' });
    const recovery = el('div', { class: 'connection-recovery' });
    const footer = el('div', { class: 'connection-footer' }, [icon('lock', 14), 'Используйте рабочий аккаунт компании.']);
    content.append(stage, error, jobStatus, recovery, footer);
    const rememberAccount = value => {
      account = value;
      if (generation !== this._generation) return;
      const index = this._accounts.findIndex(item => item.id === value.id);
      if (index < 0) this._accounts.push(value); else this._accounts[index] = value;
      this.renderAccounts();
    };
    const fetchAccount = async () => {
      if (!account) return;
      const latest = await this.api(`/api/accounts/${encodeURIComponent(account.id)}`);
      if (alive()) rememberAccount(latest);
    };
    const setBusy = value => {
      busy = value;
      content.setAttribute('aria-busy', String(value));
      for (const node of stage.querySelectorAll('button,input')) node.disabled = value;
      for (const node of recovery.querySelectorAll('button')) node.disabled = value;
    };
    const qrDetails = () => {
      const info = account?.public || {};
      const qr = info.qr_data_url;
      const expiryValue = typeof info.expires_at === 'number' && info.expires_at < 1e12 ? info.expires_at * 1000 : info.expires_at;
      const expiry = date(expiryValue);
      return { image: typeof qr === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+$/.test(qr) ? qr : '', expiry, expired: !!expiry && expiry.getTime() <= Date.now() };
    };
    const currentPhase = () => {
      if (account?.state === 'connected') return 'success';
      if (phoneEntry) return 'phone';
      if (['awaiting_password', 'password_required', 'need_password'].includes(account?.state)) return 'password';
      if (['awaiting_code', 'code_required', 'need_code'].includes(account?.state)) return 'code';
      if (['qr_required', 'awaiting_qr', 'connecting', 'starting'].includes(account?.state)) return 'qr';
      if (['action_required', 'reauth_required', 'login_required', 'failed', 'error'].includes(account?.state)) return 'restart';
      if (account?.state === 'disconnected') return 'restart';
      return supportsPhone && authMethod !== 'qr' ? 'phone' : 'qr';
    };
    const showError = (value, source = 'action') => { lastError = value?.message || String(value || ''); lastErrorSource = lastError ? source : ''; error.textContent = connectionError(value); error.hidden = !lastError; };
    const updateWhatsAppWait = () => {
      if (!whatsapp) return;
      // A client deadline describes missing progress, never provider failure or login success.
      // Repeated account refreshes do not reset it; a real QR or explicit retry does.
      if (currentPhase() !== 'qr' || qrDetails().image) { waQrWaitStartedAt = null; waQrTimedOut = false; return; }
      if (waQrWaitStartedAt === null) waQrWaitStartedAt = Date.now();
      waQrTimedOut = Date.now() - waQrWaitStartedAt >= 120000;
    };
    const recover = () => {
      recovery.replaceChildren();
      if (!account || showSuccess) return;
      const pending = this._connectionJobs.get(account.id);
      if (pending) recovery.append(button('Проверить выполнение запроса', () => runAction(pending.action), '', 'refresh'));
      else if (uncertainty) recovery.append(el('p', { class: 'field-help' }, 'Запрос мог поступить на сервер. Сначала проверим состояние, чтобы не отправлять его повторно.'), button('Проверить состояние', async () => {
        if (busy) return; setBusy(true);
        try { await fetchAccount(); if (!alive()) return; if (account?.state !== 'new' && account?.state !== 'created') uncertainty = false; showError(''); render(true); }
        catch (e) { if (alive()) showError(e); }
        finally { if (alive()) { setBusy(false); recover(); } }
      }, '', 'refresh'));
    };
    const createAccount = async config => {
      if (account) return;
      const created = await this.post('/api/accounts', { channel, label: config.phone ? `${channelName} · ${config.phone}` : channelName, config });
      if (alive()) rememberAccount(created);
    };
    const runAction = async (action, payload = {}, config = {}) => {
      if (busy || !alive()) return;
      if (whatsapp && action === 'login' && !this._connectionJobs.has(account?.id)) { waQrWaitStartedAt = Date.now(); waQrTimedOut = false; }
      setBusy(true); showError(''); recovery.replaceChildren();
      jobStatus.textContent = action === 'disconnect' ? 'Отключаем аккаунт…' : 'Подключаемся…';
      let knownJob = null;
      try {
        await createAccount(config);
        if (!alive()) return;
        knownJob = this._connectionJobs.get(account.id);
        if (!knownJob) {
          const response = await this.post(`/api/accounts/${encodeURIComponent(account.id)}/actions`, { action, payload });
          const id = response.id || response.job_id;
          if (!id) { uncertainty = true; throw new Error('Ответ сервера не содержит результата запроса. Проверьте состояние подключения.'); }
          knownJob = { id, action };
          if (generation !== this._generation) return;
          this._connectionJobs.set(account.id, knownJob);
        }
        if (!alive()) return;
        await this.pollJob(knownJob.id, status => { if (alive()) jobStatus.textContent = status === 'running' ? 'Мессенджер подтверждает подключение…' : 'Сервер готовит подключение…'; }, alive);
        this._connectionJobs.delete(account.id);
        await fetchAccount();
        if (!alive()) return;
        uncertainty = false; phoneEntry = false;
        if (knownJob.action === 'disconnect') disconnected = true;
        jobStatus.textContent = '';
        render(true);
      } catch (e) {
        if (e.jobFailed && account) this._connectionJobs.delete(account.id);
        if (!alive()) return;
        if (!knownJob && account && (!e.status || e.status >= 500)) uncertainty = true;
        // A terminal provider error may have changed the next required step.
        if (e.jobFailed) { try { await fetchAccount(); } catch {} }
        if (!alive()) return;
        render(true); showError(e); jobStatus.textContent = '';
      } finally {
        if (alive()) { setBusy(false); if (uncertainty || this._connectionJobs.has(account?.id)) for (const node of stage.querySelectorAll('button,input')) node.disabled = true; recover(); }
      }
    };
    const success = () => {
      showSuccess = true;
      modal.querySelector('.modal').classList.add('connection-complete');
      modal.querySelector('#modal-title').textContent = `${CHANNEL_LABELS[channel]} подключён`;
      const identity = account.public?.identity && typeof account.public.identity === 'object' ? account.public.identity : {};
      const name = identity.phone || (identity.username ? `@${identity.username}` : identity.display_name) || account.label;
      const hero = el('div', { class: 'connection-success-icon' }, [channelIcon(channel), el('span', { class: 'success-check' }, icon('check', 14))]);
      stage.replaceChildren(el('div', { class: 'connection-success' }, [hero, el('h3', {}, `Канал ${name} подключён`), el('p', {}, 'Теперь можно общаться с клиентами в чате.'), button('Перейти в чаты', async () => { const id = account.id; this.closeModal(); await this.selectAccount(id); }, 'primary full'), el('p', { class: 'field-help' }, 'Новые сообщения появятся в диалогах. История загружается по возможностям мессенджера.')]));
      const management = el('details', { class: 'connection-management' }, [el('summary', {}, 'Управление подключением')]);
      if (telegram) management.append(button('Загрузить последние диалоги', () => runAction('sync'), '', 'refresh'));
      management.append(button('Отключить аккаунт', () => {
        management.replaceChildren(el('p', { class: 'field-help' }, 'Сеанс будет отозван. Для новых сообщений потребуется повторный вход.'), el('div', { class: 'form-actions' }, [button('Отмена', () => render(true)), button('Отключить', () => runAction('disconnect'), 'danger')]));
      }, 'danger'));
      stage.append(management);
      footer.hidden = true; recovery.replaceChildren(); error.hidden = true;
    };
    const render = (force = false) => {
      if (!alive()) return;
      const next = currentPhase();
      const qr = qrDetails();
      updateWhatsAppWait();
      const waQrBlocked = whatsapp && !qr.image && (waQrTimedOut || !!account?.public?.error);
      const qrBlocked = waQrBlocked || (max && !qr.image && !!account?.public?.error);
      const signature = JSON.stringify([account?.state, account?.public, qr.expired, next, disconnected, waQrTimedOut]);
      if (!force && signature === lastSignature) return;
      lastSignature = signature;
      if (busy && !force) return;
      if (next === 'success') { phase = next; success(); return; }
      showSuccess = false; footer.hidden = false;
      modal.querySelector('.modal').classList.remove('connection-complete');
      modal.querySelector('#modal-title').textContent = `Подключение ${CHANNEL_LABELS[channel]}`;
      phase = next;
      if (account?.public?.phone) inputPhone = account.public.phone;
      if (account?.public?.auth_method) authMethod = account.public.auth_method;
      const preserveInput = stage.querySelector('input[name="phone"]')?.value;
      if (preserveInput) inputPhone = preserveInput;
      stage.replaceChildren();
      if (phase === 'phone') {
        const phone = this.field('phone', 'Номер телефона', 'tel', true, '+7 999 123-45-67');
        phone.input.value = inputPhone; phone.input.autocomplete = 'tel';
        phone.node.append(el('span', { class: 'field-help' }, `Укажите номер вашего рабочего аккаунта ${channelName}.`));
        const submit = el('button', { type: 'submit', class: 'button primary full' }, 'Получить код');
        stage.append(el('p', { class: 'connection-intro' }, `Войдите в ${channelName}, чтобы переписываться с клиентами из CRM.`), el('form', { onsubmit: event => {
          event.preventDefault(); if (busy) return;
          const value = phone.input.value.replace(/[\s()\-]/g, '');
          if (!/^\+[1-9][0-9]{7,14}$/.test(value)) { showError(new Error(max ? 'max_phone_invalid' : 'telegram_phone_invalid')); phone.input.focus(); return; }
          inputPhone = value; authMethod = 'phone'; runAction('login', { phone: value, ...(max ? { login_method: 'sms' } : {}) }, { phone: value });
        } }, [phone.node, submit]));
        if (supportsQr) stage.append(el('div', { class: 'connection-divider' }, 'или'), button('Подключить по QR-коду', () => { authMethod = 'qr'; phoneEntry = false; runAction('qr'); }, 'full'));
      } else if (phase === 'code' || phase === 'password') {
        const password = phase === 'password';
        const field = this.field(password ? 'password' : 'code', password ? 'Пароль двухэтапной проверки' : 'Код из сообщения', password ? 'password' : 'text', true, password ? `Ваш пароль ${channelName}` : 'Введите код');
        if (!password) { field.input.inputMode = 'numeric'; field.input.autocomplete = 'one-time-code'; field.input.pattern = '[0-9 ]{4,12}'; field.input.maxLength = 12; }
        const phone = this.field('connected-phone', 'Номер телефона', 'tel'); phone.input.value = inputPhone || `Ваш аккаунт ${channelName}`; phone.input.readOnly = true;
        const submit = el('button', { type: 'submit', class: 'button primary full' }, password ? 'Подтвердить пароль' : 'Подключить');
        stage.append(el('div', { class: 'connection-step' }, password ? 'Последний шаг · защита аккаунта' : 'Подтвердите вход'), el('p', { class: 'connection-intro' }, password ? `Для вашего аккаунта включена двухэтапная проверка. Введите пароль ${channelName}.` : max ? 'Введите код входа, полученный для вашего аккаунта MAX.' : 'Откройте Telegram и найдите сообщение с кодом входа. Способ доставки кода определяет Telegram.'), el('form', { onsubmit: event => {
          event.preventDefault(); if (busy) return; const action = password ? 'password' : 'code'; const value = password ? field.input.value : field.input.value.replace(/\s/g, ''); field.input.value = ''; runAction(action, { [action]: value });
        } }, [...(!password && inputPhone ? [phone.node] : []), field.node, submit]));
        if (!password) stage.append(button('Запросить новый код', () => runAction('login', { ...(inputPhone ? { phone: inputPhone } : {}), ...(max ? { login_method: 'sms' } : {}) }), 'subtle full'));
        else {
          if (max && typeof account?.public?.hint === 'string' && account.public.hint.trim()) stage.append(el('p', { class: 'field-help connection-help' }, `Подсказка: ${account.public.hint}`));
          stage.append(el('p', { class: 'field-help connection-help' }, `Это пароль ${channelName}, а не пароль компьютера или сервера.`));
        }
        queueMicrotask(() => { if (alive() && !busy) field.input.focus(); });
      } else if (phase === 'qr') {
        const steps = telegram ? ['Откройте Telegram на телефоне.', 'Перейдите в «Настройки» → «Устройства».', 'Нажмите «Подключить устройство» и отсканируйте QR-код.', 'Дождитесь подтверждения подключения.'] : max ? ['Откройте MAX на телефоне с рабочим аккаунтом.', 'Отсканируйте QR-код камерой телефона.', 'Перейдите в MAX и подтвердите вход.', 'Дождитесь подтверждения подключения здесь.'] : ['Откройте WhatsApp на телефоне.', 'Выберите «Настройки» → «Связанные устройства». На Android этот раздел находится в меню ⋮.', 'Нажмите «Привязка устройства» и отсканируйте QR-код.', 'Дождитесь подтверждения подключения.'];
        const visual = el('div', { class: 'qr-visual' });
        if (qr.image && !qr.expired) visual.append(el('img', { class: 'connection-qr', src: qr.image, alt: `QR-код подключения ${CHANNEL_LABELS[channel]}`, referrerpolicy: 'no-referrer' }));
        else visual.append(el('div', { class: 'qr-placeholder', role: 'status' }, [qrBlocked ? icon('warning', 28) : qr.expired ? icon('refresh', 28) : el('span', { class: 'connection-spinner' }), el('span', {}, qrBlocked ? 'QR-код пока не получен' : qr.expired ? max ? 'QR-код устарел' : 'Обновляем QR-код…' : 'Получаем QR-код…')]));
        const qrStatus = el('p', { class: 'qr-status', role: 'status' }, qrBlocked ? 'Повторите подключение или проверьте связь с сервером.' : qr.expired ? max ? 'Получите новый QR-код для входа.' : 'Предыдущий код больше не действует.' : qr.expiry ? '' : 'Подключение подтвердится автоматически.');
        if (qr.expiry && !qr.expired) qrStatus.textContent = `Код действует ещё ${Math.max(0, Math.ceil((qr.expiry.getTime() - Date.now()) / 1000))} с`;
        visual.append(qrStatus);
        stage.append(el('div', { class: 'qr-layout' }, [el('div', {}, [el('h3', { class: 'connection-subtitle' }, 'Свяжите ваш аккаунт с CRM'), el('ol', { class: 'connection-steps' }, steps.map(text => el('li', {}, text))), el('p', { class: 'connection-help' }, telegram || max ? `При включённой двухэтапной проверке ${channelName} дополнительно запросит пароль.` : 'Клиенты увидят обычный чат с вашим рабочим номером.')]), visual]));
        if (account) stage.append(el('div', { class: 'qr-actions' }, [button(qrBlocked || ['new', 'created'].includes(account.state) ? 'Повторить подключение' : 'Обновить QR-код', () => runAction(whatsapp ? 'login' : 'qr'), '', 'refresh'), supportsPhone ? button('Войти по номеру телефона', () => { authMethod = 'phone'; phoneEntry = true; showError(''); render(true); }, 'subtle') : null]));
        if (whatsapp) stage.append(el('p', { class: 'connection-caution' }, 'Подключение через связанные устройства использует неофициальную интеграцию. WhatsApp может ограничить её работу.'));
      } else {
        const lateWhatsApp = whatsapp && !disconnected && account?.state === 'action_required' && account.public?.error === 'waha_connect_timeout';
        const maxQrExpired = max && (account?.public?.reason === 'qr_expired' || account?.public?.error === 'max_qr_expired');
        stage.append(el('div', { class: 'connection-unavailable' }, [el('div', { class: 'setup-illustration' }, icon(disconnected ? 'check' : 'refresh', 28)), el('h3', {}, disconnected ? 'Аккаунт отключён' : maxQrExpired ? 'QR-код MAX устарел' : lateWhatsApp ? 'Подтверждение подключения задерживается' : 'Нужно повторить подключение'), el('p', {}, disconnected ? 'История переписки сохранена в CRM.' : maxQrExpired ? 'Получите новый QR-код и подтвердите вход в MAX на телефоне.' : lateWhatsApp ? 'Если WhatsApp завершит вход, статус обновится автоматически.' : 'Откройте приложение на телефоне и начните вход ещё раз.'), button(maxQrExpired ? 'Получить новый QR-код' : 'Подключить снова', () => { disconnected = false; if (telegram) { authMethod = 'phone'; phoneEntry = true; showError(''); render(true); } else if (max && supportsQr) { authMethod = 'qr'; phoneEntry = false; runAction('qr'); } else if (max && supportsPhone) { authMethod = 'phone'; phoneEntry = true; showError(''); render(true); } else runAction('login'); }, 'primary')]));
        if (max && account?.public?.error === 'max_account_identity_changed') {
          // The backend preserves the original identity binding. A mismatched
          // session must be explicitly revoked before this channel can reconnect.
          stage.querySelector('.connection-unavailable > .button')?.remove();
          const revoke = el('div', { class: 'connection-management' });
          revoke.append(button('Отключить аккаунт', () => revoke.replaceChildren(el('p', { class: 'field-help' }, 'Сеанс MAX будет отозван. История канала и привязка к прежнему аккаунту сохранятся.'), el('div', { class: 'form-actions' }, [button('Отмена', () => render(true)), button('Отключить', () => runAction('disconnect'), 'danger')])), 'danger full'));
          stage.append(revoke);
        }
      }
      if (whatsapp || max) {
        if (account?.public?.error) showError(new Error(account.public.error), 'provider');
        else if (waQrTimedOut && phase === 'qr') showError(waWaitMessage, 'watchdog');
        else if (['provider', 'watchdog'].includes(lastErrorSource)) showError('');
      } else if (account?.public?.error && !lastError) showError(new Error(account.public.error));
      recover();
      if (uncertainty || this._connectionJobs.has(account?.id)) for (const node of stage.querySelectorAll('button,input')) node.disabled = true;
    };
    this._modalRefresh = () => {
      if (!alive() || !account) return;
      const latest = this._accounts.find(item => item.id === account.id);
      if (latest) account = latest;
      // Older workers can report a transport error while the auth job is still
      // queued. Show it without dropping the known job or submitting a duplicate.
      if (busy && (telegram || max)) {
        if (account.public?.error) showError(new Error(account.public.error), 'provider');
        else if (lastErrorSource === 'provider') showError('');
      }
      if (!busy) render();
    };
    const expiryTimer = setInterval(() => {
      if (!alive() || busy || phase !== 'qr') return;
      if (whatsapp) { render(); return; }
      const qr = qrDetails();
      if (qr.expired) render();
      else if (qr.expiry) { const node = stage.querySelector('.qr-status'); if (node) node.textContent = `Код действует ещё ${Math.max(0, Math.ceil((qr.expiry.getTime() - Date.now()) / 1000))} с`; }
    }, 1000);
    this._modalCleanup = () => { clearInterval(expiryTimer); for (const input of content.querySelectorAll('input')) input.value = ''; };
    render(true);
    if (account && this._connectionJobs.has(account.id)) runAction(this._connectionJobs.get(account.id).action);
    else if ((!originalAccount && whatsapp) || autoLogin) runAction(max && supportsQr ? 'qr' : 'login');
    else if (!originalAccount && max && supportsQr) runAction('qr');
  }
  openManageAccount(originalAccount, autoLogin = false) {
    if (['telegram', 'whatsapp_web', 'max_personal'].includes(originalAccount.channel)) { this.openGuidedConnection(originalAccount.channel, originalAccount, autoLogin); return; }
    const content = this.openModal(originalAccount.label, `${CHANNEL_LABELS[originalAccount.channel] || originalAccount.channel} · управление подключением`);
    const modal = this._modal;
    const capability = this.capability(originalAccount.channel);
    const state = el('div', { class: 'auth-state' });
    const publicInfo = el('div');
    const jobStatus = el('div', { class: 'job-status', role: 'status' });
    const error = this.formError();
    const controls = el('div');
    const actions = capability?.actions || ['login', 'code', 'password', 'sync', 'disconnect'];
    let stateSignature = '';
    const renderState = () => {
      const account = this._accounts.find(a => a.id === originalAccount.id) || originalAccount;
      const signature = JSON.stringify([account.state, account.public]);
      if (signature === stateSignature) return;
      stateSignature = signature;
      state.replaceChildren(el('span', { class: `status-dot ${stateColor(account.state)}` }), el('span', {}, stateLabel(account.state)), modeBadge(capability, account.channel));
      publicInfo.replaceChildren();
      const info = account.public || {};
      const payload = info.result && typeof info.result === 'object' ? { ...info, ...info.result } : info;
      let qr = payload.qr_data_url || payload.qr || payload.qr_code || payload.qr_text;
      if (qr && typeof qr === 'object') qr = qr.data ? `data:${qr.mimetype || 'image/png'};base64,${qr.data}` : qr.value;
      if (typeof qr === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+$/.test(qr)) publicInfo.append(el('p', { class: 'field-help' }, 'Откройте связанные устройства в мессенджере на телефоне и отсканируйте QR-код.'), el('img', { class: 'auth-qr', alt: 'QR-код подключения рабочего аккаунта', src: qr, referrerpolicy: 'no-referrer' }));
      else if (typeof qr === 'string' && qr) publicInfo.append(el('p', { class: 'field-help' }, 'Провайдер вернул данные QR без изображения. Выполните синхронизацию или проверьте настройки шлюза.'), el('div', { class: 'auth-text' }, qr));
      for (const key of ['message', 'instructions', 'notice', 'error', 'warning']) if (payload[key]) publicInfo.append(el('p', { class: key === 'error' ? 'form-error' : 'provider-description' }, typeof payload[key] === 'string' ? payload[key] : JSON.stringify(payload[key])));
      if (payload.phone) publicInfo.append(el('p', { class: 'field-help' }, `Рабочий номер: ${payload.phone}`));
      if (['whatsapp_cloud', 'max_bot', 'sms_gateway'].includes(account.channel)) {
        if (typeof payload.webhook_path === 'string' && payload.webhook_path.startsWith('/webhooks/')) {
          const webhook = this.field('webhook-url', 'Адрес входящих сообщений (webhook)', 'url');
          webhook.input.readOnly = true;
          webhook.input.value = new URL(`${this._baseUrl}${payload.webhook_path}`, location.origin).href;
          publicInfo.append(webhook.node);
        }
        publicInfo.append(el('p', { class: 'provider-description' }, 'Администратору нужно настроить webhook и проверку подписи у провайдера. Статус «Подключён» подтверждает учётные данные. Приём входящих сообщений нужно отдельно проверить реальным тестовым сообщением.'));
        if (['whatsapp_cloud', 'max_bot'].includes(account.channel)) publicInfo.append(el('p', { class: 'field-help' }, 'Для входящих сообщений провайдеру нужен публично доступный HTTPS-адрес webhook. Локальный адрес этого компьютера для этого не подходит.'));
      }
    };
    const runAction = async (action, payload = {}) => {
      error.hidden = true;
      const buttons = Array.from(controls.querySelectorAll('button'));
      buttons.forEach(b => { b.disabled = true; });
      jobStatus.textContent = 'Задача передаётся серверу…';
      try {
        const response = await this.post(`/api/accounts/${encodeURIComponent(originalAccount.id)}/actions`, { action, payload });
        const jobId = response.id || response.job_id;
        if (!jobId) throw new Error('Сервер не вернул идентификатор задачи. Проверьте состояние аккаунта перед повтором.');
        await this.pollJob(jobId, status => { if (modal === this._modal) jobStatus.textContent = status === 'running' ? 'Провайдер выполняет запрос…' : 'Задача в очереди…'; });
        const accounts = array(await this.api('/api/accounts'));
        this._accounts = accounts; this.renderAccounts();
        if (modal === this._modal) { renderState(); jobStatus.textContent = 'Задача выполнена. Текущее состояние показано выше.'; }
      } catch (e) { if (modal === this._modal) { error.textContent = e.message; error.hidden = false; jobStatus.textContent = ''; } }
      finally { buttons.forEach(b => { b.disabled = false; }); }
    };
    const mainButtons = el('div', { class: 'form-actions' });
    if (actions.includes('login')) mainButtons.append(button('Начать подключение', () => runAction('login'), 'primary'));
    if (actions.includes('sync')) mainButtons.append(button('Проверить состояние', () => runAction('sync'), '', 'refresh'));
    controls.append(mainButtons);
    if (['telegram', 'max_personal'].includes(originalAccount.channel)) {
      for (const action of ['code', 'password'].filter(a => actions.includes(a))) {
        const field = this.field(action, action === 'code' ? `Код из ${originalAccount.channel === 'telegram' ? 'Telegram' : 'MAX'} или SMS` : 'Пароль двухэтапной проверки', action === 'password' ? 'password' : 'text', true);
        if (action === 'code') field.input.setAttribute('inputmode', 'numeric');
        const form = el('form', { class: 'auth-section', onsubmit: async event => { event.preventDefault(); const value = field.input.value; field.input.value = ''; await runAction(action, { [action]: value }); } }, [field.node, el('div', { class: 'form-actions' }, el('button', { type: 'submit', class: 'button' }, action === 'code' ? 'Подтвердить код' : 'Подтвердить пароль'))]);
        controls.append(form);
      }
    }
    if (actions.includes('disconnect')) {
      const disconnect = button('Отключить аккаунт', () => {
        disconnect.disabled = true;
        const disconnectNotice = ['whatsapp_cloud', 'max_bot', 'sms_gateway'].includes(originalAccount.channel)
          ? 'CRM остановит обработку сообщений этого аккаунта. Токен при необходимости отзовите в кабинете провайдера.'
          : 'Сеанс в мессенджере будет отозван. Для новых сообщений потребуется повторный вход.';
        const confirmation = el('div', { class: 'provider-description' }, [disconnectNotice, el('div', { class: 'form-actions' }, [button('Отмена', () => { confirmation.remove(); disconnect.disabled = false; }), button('Отключить', async () => { confirmation.remove(); await runAction('disconnect'); disconnect.disabled = false; }, 'danger')])]);
        controls.append(confirmation);
      }, 'danger');
      controls.append(el('div', { class: 'auth-section' }, disconnect));
    }
    content.append(state, capability?.description ? el('p', { class: 'provider-description' }, capability.description) : el('span'), publicInfo, error, jobStatus, controls);
    this._modalRefresh = renderState;
    renderState();
    if (autoLogin && actions.includes('login')) runAction('login');
  }
  async pollJob(id, onUpdate, isActive = () => true) {
    const key = `${this._generation}:${id}`;
    this._jobs.add(key);
    const generation = this._generation;
    const deadline = Date.now() + 135000;
    let lastStatus = '';
    try {
      for (let attempt = 0; attempt < 90 && Date.now() < deadline; attempt += 1) {
        if (generation !== this._generation || !this.isConnected || !isActive()) throw new Error('Сеанс завершён.');
        const job = await this.api(`/api/jobs/${encodeURIComponent(id)}`);
        if (!isActive() || generation !== this._generation) throw new Error('Сеанс завершён.');
        if (['done', 'completed', 'succeeded'].includes(job.status)) return job;
        if (['failed', 'error'].includes(job.status)) { const error = new Error(typeof job.error === 'string' ? job.error : job.error?.message || job.result?.error || 'Провайдер не выполнил запрос.'); error.jobFailed = true; throw error; }
        lastStatus = job.status;
        onUpdate(job.status);
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      throw new Error(lastStatus === 'queued'
        ? 'Сервер пока не начал подключение. Нажмите «Проверить выполнение запроса». Если ожидание повторится, обратитесь к администратору.'
        : 'Подключение пока не подтвердилось. Нажмите «Проверить выполнение запроса», чтобы узнать результат. Повторный запрос входа не отправляется.');
    } finally { this._jobs.delete(key); }
  }
  openRenameConversation() {
    const conversation = this.conversation; if (!conversation) return;
    const content = this.openModal('Имя клиента или компания', 'Название используется для поиска в CRM. Получатель и рабочий аккаунт остаются прежними.');
    const modal = this._modal;
    const title = this.field('conversation-title', 'Имя клиента или компания', 'text', true, 'ООО Ромашка'); title.input.value = conversation.title || ''; title.input.maxLength = 256;
    const error = this.formError(); const submit = el('button', { type: 'submit', class: 'button primary' }, 'Сохранить');
    content.append(el('form', { onsubmit: async event => {
      event.preventDefault(); if (submit.disabled || !title.input.value.trim()) return; submit.disabled = true; error.hidden = true;
      try {
        const result = await this.api(`/api/conversations/${encodeURIComponent(conversation.id)}`, { method: 'PATCH', body: JSON.stringify({ title: title.input.value.trim() }) });
        if (modal !== this._modal) return; this.updateConversation(result); this.closeModal(); this.renderConversations(); if (this._selectedConversation === conversation.id) this.renderChat();
      } catch (e) { if (modal === this._modal) { error.textContent = e.message; error.hidden = false; submit.disabled = false; } }
    } }, [title.node, error, el('div', { class: 'form-actions' }, [button('Отмена', () => this.closeModal()), submit])]));
    title.input.focus(); title.input.select();
  }
  openNewConversation() {
    if (!this._accounts.length) { this.openConnect(); return; }
    const content = this.openModal('Новый диалог', 'Найдите клиента или компанию, затем выберите мессенджер и рабочий аккаунт.');
    this._modal.querySelector('.modal').classList.add('recipient-modal');
    const modal = this._modal, generation = this._generation;
    const alive = () => this._modal === modal && this._generation === generation;
    let request = 0, timer, offset = 0, chosen = null, routes = [];
    const search = this.field('recipient-search', 'Клиент или компания', 'text', false, 'Например, ООО Ромашка');
    const channel = el('select', { 'aria-label': 'Мессенджер получателя' }, [el('option', { value: '' }, 'Все мессенджеры'), ...[...new Set(this._accounts.map(account => account.channel))].map(value => el('option', { value }, CHANNEL_LABELS[value] || value))]);
    const account = el('select', { 'aria-label': 'Аккаунт отправителя' });
    const list = el('div', { class: 'recipient-results', 'aria-label': 'Найденные диалоги' });
    const pagination = el('div', { class: 'recipient-pagination' });
    const status = el('div', { class: 'job-status', role: 'status' });
    const selection = el('p', { class: 'recipient-selection', hidden: true });
    const submit = button('Открыть диалог', async () => { if (!chosen) return; const route = chosen; this.closeModal(); await this.selectConversation(route.id, route); this._composer?.focus(); }, 'primary'); submit.disabled = true;
    const resetSelection = () => { chosen = null; routes = []; submit.disabled = true; selection.hidden = true; list.replaceChildren(); pagination.replaceChildren(); };
    const renderAccounts = () => { account.replaceChildren(el('option', { value: '' }, 'Все рабочие аккаунты'), ...this._accounts.filter(item => !channel.value || item.channel === channel.value).map(item => el('option', { value: item.id }, `${item.label} · ${CHANNEL_LABELS[item.channel] || item.channel}`))); };
    const renderRoutes = () => {
      list.replaceChildren(...routes.map(route => {
        const owner = this._accounts.find(item => item.id === route.account_id);
        const messenger = route.channel || owner?.channel;
        return el('button', { type: 'button', class: `recipient-route${chosen?.id === route.id ? ' selected' : ''}`, 'aria-pressed': String(chosen?.id === route.id), onclick: () => {
          chosen = route; submit.disabled = false; selection.hidden = false;
          selection.textContent = `Открыть ${route.title || route.peer} в ${CHANNEL_LABELS[messenger] || messenger} от аккаунта «${route.account_label || owner?.label || ''}»`;
          renderRoutes();
        } }, [el('div', { class: 'avatar' }, initials(route.title || route.peer)), el('div', { class: 'recipient-route-copy' }, [el('strong', {}, route.title || route.peer), el('span', {}, route.peer), el('small', {}, `${CHANNEL_LABELS[messenger] || messenger} · ${route.account_label || owner?.label || ''}`)]), channelIcon(messenger), chosen?.id === route.id ? icon('check', 17) : null]);
      }));
      if (!routes.length) list.append(emptyState('Сохранённых диалогов не найдено', 'Уточните название или номер. Для нового клиента укажите телефон или username.', null, true));
    };
    const load = async () => {
      const current = ++request;
      status.textContent = 'Ищем диалоги…';
      const params = new URLSearchParams({ q: search.input.value.trim(), limit: '20', offset: String(offset) });
      if (channel.value) params.set('channel', channel.value);
      if (account.value) params.set('account_id', account.value);
      try {
        const result = await this.api(`/api/recipients?${params}`);
        if (!alive() || current !== request) return;
        routes = array(result); status.textContent = `Найдено диалогов: ${result.total ?? routes.length}`; renderRoutes();
        const previous = button('Назад', () => { offset = Math.max(0, offset - 20); resetSelection(); load(); }, 'subtle'); previous.disabled = !offset;
        const next = button('Далее', () => { offset += 20; resetSelection(); load(); }, 'subtle'); next.disabled = !result.has_more;
        pagination.replaceChildren(previous, el('span', {}, routes.length ? `${offset + 1}–${offset + routes.length}` : '0'), next);
      } catch (error) { if (alive() && current === request) status.replaceChildren(el('span', {}, error.message), button('Повторить поиск', load, 'subtle', 'refresh')); }
    };
    search.input.addEventListener('input', () => { resetSelection(); offset = 0; request += 1; clearTimeout(timer); timer = setTimeout(load, 250); });
    channel.addEventListener('change', () => { resetSelection(); offset = 0; renderAccounts(); load(); });
    account.addEventListener('change', () => { resetSelection(); offset = 0; load(); });
    renderAccounts();
    content.append(search.node, el('div', { class: 'recipient-filters' }, [el('label', { class: 'field' }, [el('span', {}, 'Мессенджер'), channel]), el('label', { class: 'field' }, [el('span', {}, 'Рабочий аккаунт'), account])]), status, list, pagination, selection, el('div', { class: 'form-actions' }, [button('Новый получатель по номеру или username', () => this.openDirectConversation(search.input.value.trim()), 'subtle'), submit]));
    this._modalCleanup = () => clearTimeout(timer);
    load(); search.input.focus();
  }
  openDirectConversation(suggestedTitle = '') {
    const content = this.openModal('Новый получатель', 'Выберите, откуда написать, и укажите номер или username клиента.');
    const modal = this._modal, generation = this._generation;
    const channels = [...new Set(this._accounts.map(account => account.channel))];
    const channel = el('select', { 'aria-label': 'Мессенджер нового диалога', required: true }, [el('option', { value: '' }, 'Выберите мессенджер'), ...channels.map(value => el('option', { value }, CHANNEL_LABELS[value] || value))]);
    const sender = el('select', { 'aria-label': 'Рабочий аккаунт нового диалога', required: true, disabled: true }, el('option', { value: '' }, 'Выберите аккаунт'));
    const peer = this.field('peer', 'Телефон или username клиента', 'text', true, '+79991234567 или @username'); peer.input.disabled = true;
    const title = this.field('title', 'Имя клиента или компания', 'text', false, 'ООО Ромашка'); title.input.value = suggestedTitle; title.input.maxLength = 256;
    const help = el('p', { class: 'field-help recipient-help' }, 'Для существующего диалога можно найти клиента по имени в предыдущем окне.');
    const error = this.formError(); let busy = false;
    const submit = el('button', { type: 'submit', class: 'button primary', disabled: true }, 'Открыть диалог');
    const hints = { telegram: '@username или телефон +79991234567', whatsapp_web: 'Телефон в международном формате: +79991234567', whatsapp_cloud: 'Телефон в международном формате: +79991234567', max_personal: 'Телефон +79991234567, user:123456 или ID существующего чата', max_bot: 'ID чата MAX. Клиент должен сначала написать боту.', sms_gateway: 'Телефон в международном формате: +79991234567', demo: 'Идентификатор тестового клиента, например client-001' };
    channel.addEventListener('change', () => { sender.replaceChildren(el('option', { value: '' }, 'Выберите аккаунт'), ...this._accounts.filter(account => account.channel === channel.value).map(account => el('option', { value: account.id }, `${account.label} · ${stateLabel(account.state)}`))); sender.disabled = !channel.value; peer.input.disabled = true; submit.disabled = true; help.textContent = hints[channel.value] || ''; });
    sender.addEventListener('change', () => { peer.input.disabled = !sender.value; submit.disabled = !sender.value; });
    content.append(el('form', { onsubmit: async event => {
      event.preventDefault(); if (busy || submit.disabled) return; const account = this._accounts.find(item => item.id === sender.value && item.channel === channel.value); if (!account) return;
      busy = true; submit.disabled = true; channel.disabled = true; sender.disabled = true; peer.input.disabled = true; title.input.disabled = true; error.hidden = true;
      try {
        const conversation = await this.post('/api/conversations', { account_id: account.id, peer: peer.input.value.trim(), title: title.input.value.trim() });
        if (modal !== this._modal || generation !== this._generation) return;
        this.closeModal(); await this.selectConversation(conversation.id, { ...conversation, channel: account.channel, account_label: account.label, account_state: account.state }); this.loadInbox(true); this._composer?.focus();
      } catch (e) { if (modal === this._modal) { busy = false; channel.disabled = false; sender.disabled = false; peer.input.disabled = false; title.input.disabled = false; error.textContent = e.message; error.hidden = false; submit.disabled = false; } }
    } }, [el('div', { class: 'recipient-filters' }, [el('label', { class: 'field' }, [el('span', {}, 'Мессенджер'), channel]), el('label', { class: 'field' }, [el('span', {}, 'Рабочий аккаунт'), sender])]), peer.node, help, title.node, error, el('div', { class: 'form-actions' }, [button('К поиску клиентов', () => this.openNewConversation()), submit])]));
  }
  openHistoryImport() {
    const eligible = this._accounts.filter(account => account.state === 'connected' && ['telegram', 'whatsapp_web', 'max_personal'].includes(account.channel));
    const content = this.openModal('Загрузить историю', 'Импорт доступных диалогов и сообщений из рабочего аккаунта.');
    const modal = this._modal, generation = this._generation, alive = () => modal === this._modal && generation === this._generation;
    const sender = el('select', { 'aria-label': 'Аккаунт для загрузки истории', required: true }, [el('option', { value: '' }, 'Выберите рабочий аккаунт'), ...eligible.map(account => el('option', { value: account.id }, `${account.label} · ${CHANNEL_LABELS[account.channel]}`))]);
    if (eligible.some(account => account.id === this._filterAccountId)) sender.value = this._filterAccountId;
    const depth = el('select', { 'aria-label': 'Период истории' }, [el('option', { value: '30' }, 'Последние 30 дней'), el('option', { value: '365' }, 'Последний год'), el('option', { value: '3650' }, 'Доступная история за последние 10 лет')]); depth.value = '3650';
    const status = el('div', { class: 'history-progress', role: 'status', 'aria-live': 'polite' });
    const error = this.formError(); let busy = false;
    const submit = button('Загрузить историю', async () => {
      if (busy || !sender.value) return; busy = true; submit.disabled = true; sender.disabled = true; depth.disabled = true; error.hidden = true;
      const id = sender.value;
      try {
        let jobId = this._historyJobs.get(id);
        if (!jobId) { const job = await this.post(`/api/accounts/${encodeURIComponent(id)}/actions`, { action: 'history', payload: { history_days: Number(depth.value) } }); jobId = job.id || job.job_id; if (!jobId) throw new Error('Сервер не вернул идентификатор запроса истории.'); this._historyJobs.set(id, jobId); }
        await this.pollJob(jobId, () => { if (alive()) status.textContent = 'Сервер готовит загрузку истории…'; }, alive);
        this._historyJobs.delete(id);
        const accounts = array(await this.api('/api/accounts')); if (!alive()) return;
        this._accounts = accounts; status.textContent = 'Запрос принят. Загружаем доступную историю…'; renderProgress(); await this.loadInbox(true);
      } catch (e) { if (e.jobFailed) this._historyJobs.delete(id); if (alive()) { error.textContent = ['history_not_supported', 'history_sync_not_supported'].includes(e.message) ? 'Этот канал не поддерживает загрузку истории.' : e.message === 'account_not_connected' ? 'Сначала подключите рабочий аккаунт.' : e.message; error.hidden = false; } }
      finally { if (alive()) { busy = false; sender.disabled = false; depth.disabled = false; renderProgress(); } }
    }, 'primary');
    const renderProgress = () => {
      const account = this._accounts.find(item => item.id === sender.value), progress = account?.public?.history_sync;
      if (!busy && progress) {
        const done = Number(progress.chats_completed || 0), total = Number(progress.chats_discovered || 0);
        const title = progress.state === 'complete' ? 'Загрузка доступной истории завершена.' : progress.state === 'partial' ? 'Загружена доступная часть истории.' : 'История загружается…';
        const finished = ['complete', 'partial'].includes(progress.state);
        const detail = progress.reason === 'waha_device_history_only' ? finished ? 'Загружена доступная на подключённом устройстве история.' : 'WhatsApp передаёт историю с подключённого устройства.' : progress.reason === 'max_catalog_pagination_unavailable' ? finished ? 'История найденных чатов загружена; MAX может передать не все старые чаты.' : 'MAX передаёт историю найденных чатов; некоторые старые чаты могут быть недоступны.' : progress.reason === 'history_fetch_failed' ? 'Не удалось загрузить часть истории. Повторите загрузку; сохранённые сообщения останутся в CRM.' : '';
        status.replaceChildren(el('strong', {}, title), el('span', {}, `Обработано диалогов: ${done}${total ? ` из ${total}` : ''}`), detail ? el('span', {}, detail) : null);
      }
      const sameImport = progress?.state === 'importing' && Number(progress.history_days) === Number(depth.value);
      submit.disabled = busy || !sender.value || (sameImport && !this._historyJobs.has(sender.value));
      if (!busy) submit.textContent = this._historyJobs.has(sender.value) ? 'Проверить запрос истории' : sameImport ? 'История загружается…' : 'Загрузить историю';
    };
    sender.addEventListener('change', () => { status.textContent = ''; error.hidden = true; renderProgress(); });
    depth.addEventListener('change', renderProgress);
    content.append(el('label', { class: 'field' }, [el('span', {}, 'Рабочий аккаунт'), sender]), el('label', { class: 'field' }, [el('span', {}, 'Период'), depth]), el('p', { class: 'provider-description' }, 'Мессенджер определяет, какая история доступна. Импорт идёт в фоне; окно можно закрыть. Уже сохранённые сообщения останутся в CRM.'), status, error, el('div', { class: 'form-actions' }, [button('Закрыть', () => this.closeModal()), submit]));
    this._modalRefresh = renderProgress; renderProgress();
  }
  openDemoInbound() {
    const id = this._selectedConversation;
    if (!id || this.account?.channel !== 'demo') return;
    const content = this.openModal('Тестовое входящее сообщение', 'Сообщение появится в этом демо-диалоге от имени тестового клиента.');
    const field = el('textarea', { name: 'text', rows: 3, required: true, placeholder: 'Например: Здравствуйте! Пришлите, пожалуйста, предложение.', 'aria-label': 'Текст тестового ответа' });
    const error = this.formError();
    const submit = el('button', { class: 'button primary', type: 'submit' }, 'Добавить тестовый ответ');
    content.append(el('form', { onsubmit: async event => {
      event.preventDefault(); if (!field.value.trim()) return; submit.disabled = true;
      try { await this.post('/api/demo/inbound', { conversation_id: id, text: field.value.trim() }); this.closeModal(); await this.refresh(); }
      catch (e) { error.textContent = e.message; error.hidden = false; submit.disabled = false; }
    } }, [el('div', { class: 'field' }, field), error, el('div', { class: 'form-actions' }, submit)]));
    field.focus();
  }
}

if (!customElements.get('omni-inbox')) customElements.define('omni-inbox', OmniInbox);
