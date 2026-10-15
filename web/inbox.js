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
    telegram_account_identity_changed: 'В этом канале был другой аккаунт. Для нового номера добавьте отдельный канал.',
    whatsapp_account_identity_changed: 'В этом канале был другой аккаунт. Для нового номера добавьте отдельный канал.',
    waha_transport_error: 'Нет связи со шлюзом WhatsApp. Повторите проверку через некоторое время.',
    waha_server_error: 'Шлюз WhatsApp временно недоступен. Повторите проверку через некоторое время.',
    waha_rate_limited: 'WhatsApp просит подождать перед следующей попыткой.',
    waha_request_rejected: 'WhatsApp не подтвердил запрос. Проверьте связанные устройства на телефоне и повторите попытку.',
    waha_connect_timeout: 'WhatsApp не завершил подключение за 2 минуты. Повторите подключение. Если ожидание повторится, попросите администратора проверить связь сервера с WhatsApp.',
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
    this.closeModal();
    if (this.isConnected) this._token ? this.initialize() : this.renderLogin();
  }
  get baseUrl() { return this._baseUrl; }
  set baseUrl(value) { this._baseUrl = String(value || '').replace(/\/+$/, ''); }
  get account() { return this._accounts.find(a => a.id === this._selectedAccount); }
  get conversation() { return this._conversations.find(c => c.id === this._selectedConversation); }
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
      if (this._accounts.length) await this.selectAccount(this._accounts[0].id);
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
    const identity = el('div', { class: 'identity' }, [el('div', { class: 'avatar' }, initials(identityName)), el('div', {}, [el('div', { class: 'identity-name', title: identityName }, identityName), el('div', { class: 'identity-role' }, this._identity.role === 'admin' ? 'Администратор' : 'Менеджер')])]);
    header.append(el('div', { class: 'topbar-end' }, [el('div', { class: 'connection-pill' }, [el('span', { class: 'status-dot green' }), this._connectionLabel]), identity, iconButton('logout', 'Выйти', () => { this.token = ''; this.dispatchEvent(new CustomEvent('omni-logout', { bubbles: true, composed: true })); })]));
    this._errorBanner = el('div', { class: 'error-banner', role: 'alert', hidden: true });
    this._accountList = el('div', { class: 'account-list' });
    this._accountPanel = el('aside', { class: 'accounts-panel', 'aria-label': 'Рабочие аккаунты' }, [el('div', { class: 'panel-heading' }, [el('h2', {}, 'Каналы'), iconButton('close', 'Закрыть каналы', () => this._workspace.classList.remove('accounts-open'), 'mobile-accounts')]), el('div', { class: 'eyebrow account-group-label' }, 'Мои аккаунты'), this._accountList, el('div', { class: 'account-bottom' }, [button('Подключить канал', () => this.openConnect(), 'full', 'plus'), el('p', { class: 'account-tip' }, 'Клиент общается с вашим рабочим аккаунтом. История остаётся в компании.')])]);
    this._conversationCount = el('span', { class: 'count' }, '0');
    this._search = el('input', { type: 'search', placeholder: 'Найти диалог', 'aria-label': 'Найти диалог', oninput: () => this.renderConversations() });
    this._conversationList = el('div', { class: 'conversation-list' });
    this._conversationFooter = el('div', { class: 'conversation-footer' });
    const conversations = el('section', { class: 'conversations-panel', 'aria-label': 'Список диалогов' }, [el('div', { class: 'panel-heading' }, [el('div', { class: 'heading-group' }, [iconButton('settings', 'Выбрать канал', () => this._workspace.classList.add('accounts-open'), 'mobile-accounts'), el('h2', {}, 'Диалоги'), this._conversationCount]), iconButton('plus', 'Новый диалог', () => this.openNewConversation())]), el('div', { class: 'search' }, [icon('search', 15), this._search]), this._conversationList, this._conversationFooter]);
    this._chatPanel = el('section', { class: 'chat-panel', 'aria-label': 'Переписка' });
    this._workspace = el('main', { class: 'workspace' }, [this._accountPanel, conversations, this._chatPanel]);
    this._root.replaceChildren(el('div', { class: 'app' }, [header, this._errorBanner, this._workspace]));
    this.renderAccounts(); this.renderConversations(); this.renderChat();
  }
  showError(message) {
    if (!this._errorBanner?.isConnected) return;
    this._errorBanner.replaceChildren(el('span', {}, message), iconButton('close', 'Закрыть уведомление', () => { this._errorBanner.hidden = true; }));
    this._errorBanner.hidden = false;
  }
  renderAccounts() {
    this._accountList.replaceChildren(...this._accounts.map(account => el('button', { type: 'button', class: `account-button${this._selectedAccount === account.id ? ' selected' : ''}`, 'aria-pressed': String(this._selectedAccount === account.id), onclick: () => this.selectAccount(account.id) }, [channelIcon(account.channel), el('div', { class: 'account-text' }, [el('div', { class: 'account-label' }, account.label), el('div', { class: 'account-caption' }, [el('span', { class: `status-dot ${stateColor(account.state)}` }), el('span', {}, stateLabel(account.state))])])])));
    if (!this._accounts.length) this._accountList.append(emptyState('Пока нет каналов', 'Подключите рабочий аккаунт, чтобы начать переписку.', null, true));
    if (this.account) this._conversationFooter.replaceChildren(el('span', { class: `status-dot ${stateColor(this.account.state)}` }), el('span', {}, `${CHANNEL_LABELS[this.account.channel] || this.account.channel} · ${stateLabel(this.account.state)}`), iconButton('settings', 'Управлять подключением', () => this.openManageAccount(this.account)));
  }
  async selectAccount(id) {
    if (this._composer && this._selectedConversation) this._drafts.set(this._selectedConversation, this._composer.value);
    this._selectedAccount = id;
    this._selectedConversation = null;
    this._conversations = [];
    this._messages = [];
    this._search.value = '';
    this._workspace.classList.remove('accounts-open', 'chat-open');
    this.renderAccounts(); this.renderConversations(); this.renderChat();
    try { const result = await this.api(`/api/conversations?account_id=${encodeURIComponent(id)}`); if (this._selectedAccount !== id) return; this._conversations = array(result); this.renderConversations(); }
    catch (error) { this.showError(error.message); }
  }
  renderConversations() {
    this._conversationCount.textContent = this._conversations.length;
    const query = this._search.value.trim().toLocaleLowerCase('ru');
    const conversations = this._conversations.filter(c => `${c.title} ${c.peer}`.toLocaleLowerCase('ru').includes(query)).sort((a, b) => (date(b.last_message_at)?.getTime() || 0) - (date(a.last_message_at)?.getTime() || 0));
    this._conversationList.replaceChildren(...conversations.map(conversation => el('button', { type: 'button', class: `conversation-button${conversation.id === this._selectedConversation ? ' selected' : ''}`, 'aria-pressed': String(conversation.id === this._selectedConversation), onclick: () => this.selectConversation(conversation.id) }, [el('div', { class: 'avatar large' }, initials(conversation.title || conversation.peer)), el('div', { class: 'conversation-copy' }, [el('div', { class: 'conversation-topline' }, [el('span', { class: 'conversation-name' }, conversation.title || conversation.peer), el('time', { class: 'conversation-time', title: dateLabel(conversation.last_message_at) }, timeLabel(conversation.last_message_at))]), el('div', { class: 'conversation-preview' }, conversation.last_message?.text || conversation.peer)])])));
    if (!conversations.length) this._conversationList.append(emptyState(query ? 'Ничего не найдено' : 'Здесь начнутся диалоги', query ? 'Попробуйте другое имя или номер.' : 'Новый диалог появится после входящего сообщения. Или создайте его сами.', !query && this.account ? button('Новый диалог', () => this.openNewConversation(), '', 'plus') : null, true));
  }
  async selectConversation(id) {
    if (this._composer && this._selectedConversation) this._drafts.set(this._selectedConversation, this._composer.value);
    this._selectedConversation = id;
    this._messages = [];
    this._hasOlder = false;
    this._messagesLoading = true;
    this._workspace.classList.add('chat-open');
    this.renderConversations(); this.renderChat();
    try {
      const result = array(await this.api(`/api/conversations/${encodeURIComponent(id)}/messages?limit=100`));
      if (this._selectedConversation !== id) return;
      this._messages = result; this._hasOlder = result.length >= 100; this._messagesLoading = false; this.renderMessages(true);
    } catch (error) { if (this._selectedConversation === id) { this._messagesLoading = false; this.renderMessages(); this.showError(error.message); } }
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
    const capability = this.capability(account.channel);
    this._chatHeaderState = el('span', { class: `status-dot ${stateColor(account.state)}` });
    const header = el('header', { class: 'chat-header' }, [iconButton('arrow', 'К списку диалогов', () => this._workspace.classList.remove('chat-open'), 'mobile-back'), el('div', { class: 'avatar large' }, initials(conversation.title || conversation.peer)), el('div', { class: 'chat-heading' }, [el('h2', {}, conversation.title || conversation.peer), el('div', { class: 'chat-subtitle' }, [this._chatHeaderState, el('span', {}, `${CHANNEL_LABELS[account.channel] || account.channel} · ${conversation.peer}`)])]), modeBadge(capability, account.channel), iconButton('settings', 'Настроить аккаунт', () => this.openManageAccount(this.account))]);
    let notice = '';
    if (account.channel === 'demo') notice = 'Тестовый канал: сообщения не отправляются во внешние мессенджеры.';
    else if (account.channel === 'whatsapp_cloud') notice = 'Свободный текст доступен в течение 24 часов после сообщения клиента. Вне этого окна выберите одобренный шаблон.';
    else if (account.channel === 'max_bot') notice = 'Сообщения отправляются от бота компании. Клиент должен сначала начать диалог с ботом.';
    else if (capability?.mode && /experimental|unofficial/.test(capability.mode)) notice = 'Экспериментальная интеграция. Отслеживайте состояние подключения и подтверждение отправки.';
    this._messageList = el('div', { class: 'messages', role: 'log', 'aria-label': 'История сообщений', 'aria-live': 'polite', 'aria-relevant': 'additions' });
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
      nodes.push(el('div', { class: `message-row${outbound ? ' outbound' : ''}` }, content));
    }
    if (!nodes.length) nodes.push(emptyState(this._messagesLoading ? 'Загружаем историю' : 'Начните разговор', this._messagesLoading ? 'Получаем сохранённые сообщения…' : 'Сообщения этого диалога появятся здесь.', null, true));
    list.replaceChildren(...nodes);
    if (scrollToBottom || nearBottom) list.scrollTop = list.scrollHeight;
    else list.scrollTop = oldTop;
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
    const accountId = this._selectedAccount, conversationId = this._selectedConversation, generation = this._generation;
    try {
      const requests = [this.api('/api/accounts')];
      if (accountId) requests.push(this.api(`/api/conversations?account_id=${encodeURIComponent(accountId)}`));
      if (conversationId) requests.push(this.api(`/api/conversations/${encodeURIComponent(conversationId)}/messages?limit=100`));
      const results = await Promise.all(requests);
      if (generation !== this._generation) return;
      this._accounts = array(results[0]); this.renderAccounts(); this._modalRefresh?.();
      if (accountId === this._selectedAccount && accountId) { this._conversations = array(results[1]); this.renderConversations(); }
      if (conversationId === this._selectedConversation && conversationId) {
        const fresh = array(results[2]);
        const freshIds = new Set(fresh.map(m => m.id));
        const merged = [...this._messages.filter(m => !freshIds.has(m.id)), ...fresh].sort((a, b) => (date(a.created_at)?.getTime() || 0) - (date(b.created_at)?.getTime() || 0) || Number(a.id) - Number(b.id));
        if (JSON.stringify(merged) !== JSON.stringify(this._messages)) { this._messages = merged; this.renderMessages(); }
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
    const connected = this._accounts.filter(account => ['telegram', 'whatsapp_web'].includes(account.channel));
    if (connected.length) {
      content.append(el('h3', { class: 'channels-section-title' }, 'Ваши подключения'), el('div', { class: 'connected-channels' }, connected.map(account => el('button', { type: 'button', class: 'connected-channel', onclick: () => this.openManageAccount(account) }, [channelIcon(account.channel), el('div', { class: 'connected-channel-copy' }, [el('strong', {}, account.public?.identity?.phone || account.label), el('span', {}, CHANNEL_LABELS[account.channel])]), el('span', { class: `channel-state ${stateColor(account.state)}` }, [el('span', { class: `status-dot ${stateColor(account.state)}` }), stateLabel(account.state)])]))));
    }
    content.append(el('h3', { class: 'channels-section-title' }, connected.length ? 'Подключить ещё' : 'Выберите мессенджер'));
    const grid = el('div', { class: 'provider-grid connection-grid' });
    const order = ['whatsapp_web', 'telegram', 'max_personal', 'whatsapp_cloud', 'max_bot', 'sms_gateway', 'demo'];
    const descriptions = { whatsapp_web: 'Ваш номер · вход по QR-коду', telegram: 'Ваш аккаунт · номер телефона или QR', max_personal: 'Ваш аккаунт MAX', whatsapp_cloud: 'Бизнес-номер компании', max_bot: 'Сообщения от бота', sms_gateway: 'Сообщения с рабочей SIM', demo: 'Проверка без реальных сообщений' };
    for (const capability of [...this._capabilities].sort((a, b) => order.indexOf(a.channel) - order.indexOf(b.channel))) {
      if (capability.channel === 'demo' && !capability.enabled) continue;
      const guided = ['telegram', 'whatsapp_web'].includes(capability.channel);
      const configured = capability.enabled && capability.connection?.configured !== false;
      const description = capability.channel === 'telegram' && !capability.connection?.methods?.includes('qr') ? 'Ваш аккаунт · номер телефона и код' : descriptions[capability.channel];
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
    const telegram = channel === 'telegram';
    const capability = this.capability(channel);
    const content = this.openModal(`Подключение ${CHANNEL_LABELS[channel]}`);
    const modal = this._modal, generation = this._generation;
    const alive = () => modal === this._modal && generation === this._generation && this.isConnected;
    modal.querySelector('.modal').classList.add('connection-modal', telegram ? 'telegram-connection' : 'whatsapp-connection');
    modal.querySelector('.modal-header').prepend(channelIcon(channel));
    const unavailable = !capability?.enabled || capability.connection?.configured === false;
    if (unavailable && !originalAccount) {
      content.append(el('div', { class: 'connection-unavailable' }, [el('div', { class: 'setup-illustration' }, icon('settings', 30)), el('h3', {}, 'Нужна настройка подключения'), el('p', {}, telegram ? 'Администратор должен один раз настроить Telegram для команды. После этого вы сможете войти по номеру телефона или QR-коду.' : 'Администратор должен включить подключение WhatsApp на сервере.'), el('div', { class: 'form-actions' }, [button('Все каналы', () => this.openConnect()), telegram && this._identity.role === 'admin' ? button('Настроить Telegram', () => this.openTelegramSettings(), 'primary') : null])]));
      return;
    }
    let account = originalAccount, phase = telegram ? 'phone' : 'qr', busy = false, lastSignature = '', inputPhone = originalAccount?.public?.phone || '', authMethod = originalAccount?.public?.auth_method || (telegram ? 'phone' : 'qr');
    let showSuccess = false, lastError = '', lastErrorSource = '', disconnected = false, uncertainty = false, phoneEntry = telegram && !originalAccount;
    let waQrWaitStartedAt = telegram ? null : Date.now(), waQrTimedOut = false;
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
      const expiry = date(info.expires_at);
      return { image: typeof qr === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+$/.test(qr) ? qr : '', expiry, expired: !!expiry && expiry.getTime() <= Date.now() };
    };
    const currentPhase = () => {
      if (account?.state === 'connected') return 'success';
      if (phoneEntry) return 'phone';
      if (['awaiting_password', 'password_required', 'need_password'].includes(account?.state)) return 'password';
      if (['awaiting_code', 'code_required', 'need_code'].includes(account?.state)) return 'code';
      if (['qr_required', 'awaiting_qr', 'connecting', 'starting'].includes(account?.state)) return 'qr';
      if (['action_required', 'reauth_required', 'failed', 'error'].includes(account?.state)) return 'restart';
      if (account?.state === 'disconnected') return 'restart';
      return telegram && authMethod !== 'qr' ? 'phone' : 'qr';
    };
    const showError = (value, source = 'action') => { lastError = value?.message || String(value || ''); lastErrorSource = lastError ? source : ''; error.textContent = connectionError(value); error.hidden = !lastError; };
    const updateWhatsAppWait = () => {
      if (telegram) return;
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
      const created = await this.post('/api/accounts', { channel, label: telegram && config.phone ? `Telegram · ${config.phone}` : CHANNEL_LABELS[channel], config });
      if (alive()) rememberAccount(created);
    };
    const runAction = async (action, payload = {}, config = {}) => {
      if (busy || !alive()) return;
      if (!telegram && action === 'login' && !this._connectionJobs.has(account?.id)) { waQrWaitStartedAt = Date.now(); waQrTimedOut = false; }
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
        await this.pollJob(knownJob.id, status => { if (alive()) jobStatus.textContent = status === 'running' ? 'Мессенджер подтверждает подключение…' : 'Ожидаем подключения…'; }, alive);
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
      const waQrBlocked = !telegram && !qr.image && (waQrTimedOut || !!account?.public?.error);
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
        phone.node.append(el('span', { class: 'field-help' }, 'Укажите номер вашего рабочего аккаунта Telegram.'));
        const submit = el('button', { type: 'submit', class: 'button primary full' }, 'Получить код');
        stage.append(el('p', { class: 'connection-intro' }, 'Войдите в Telegram, чтобы переписываться с клиентами из CRM.'), el('form', { onsubmit: event => {
          event.preventDefault(); if (busy) return;
          const value = phone.input.value.replace(/[\s()\-]/g, '');
          if (!/^\+[1-9][0-9]{7,14}$/.test(value)) { showError(new Error('telegram_phone_invalid')); phone.input.focus(); return; }
          inputPhone = value; authMethod = 'phone'; runAction('login', { phone: value }, { phone: value });
        } }, [phone.node, submit]));
        if (capability.connection?.methods?.includes('qr')) stage.append(el('div', { class: 'connection-divider' }, 'или'), button('Подключить по QR-коду', () => { authMethod = 'qr'; phoneEntry = false; runAction('qr'); }, 'full'));
      } else if (phase === 'code' || phase === 'password') {
        const password = phase === 'password';
        const field = this.field(password ? 'password' : 'code', password ? 'Пароль двухэтапной проверки' : 'Код из сообщения', password ? 'password' : 'text', true, password ? 'Ваш пароль Telegram' : 'Введите код');
        if (!password) { field.input.inputMode = 'numeric'; field.input.autocomplete = 'one-time-code'; field.input.pattern = '[0-9 ]{4,12}'; field.input.maxLength = 12; }
        const phone = this.field('connected-phone', 'Номер телефона', 'tel'); phone.input.value = inputPhone || 'Ваш аккаунт Telegram'; phone.input.readOnly = true;
        const submit = el('button', { type: 'submit', class: 'button primary full' }, password ? 'Подтвердить пароль' : 'Подключить');
        stage.append(el('div', { class: 'connection-step' }, password ? 'Последний шаг · защита аккаунта' : 'Подтвердите вход'), el('p', { class: 'connection-intro' }, password ? 'Для вашего аккаунта включена двухэтапная проверка. Введите облачный пароль Telegram.' : 'Откройте Telegram и найдите сообщение с кодом входа. Способ доставки кода определяет Telegram.'), el('form', { onsubmit: event => {
          event.preventDefault(); if (busy) return; const action = password ? 'password' : 'code'; const value = password ? field.input.value : field.input.value.replace(/\s/g, ''); field.input.value = ''; runAction(action, { [action]: value });
        } }, [...(!password && inputPhone ? [phone.node] : []), field.node, submit]));
        if (!password) stage.append(button('Запросить новый код', () => runAction('login', inputPhone ? { phone: inputPhone } : {}), 'subtle full'));
        else stage.append(el('p', { class: 'field-help connection-help' }, 'Это пароль Telegram, а не пароль компьютера или сервера.'));
        queueMicrotask(() => { if (alive() && !busy) field.input.focus(); });
      } else if (phase === 'qr') {
        const steps = telegram ? ['Откройте Telegram на телефоне.', 'Перейдите в «Настройки» → «Устройства».', 'Нажмите «Подключить устройство» и отсканируйте QR-код.', 'Дождитесь подтверждения подключения.'] : ['Откройте WhatsApp на телефоне.', 'Выберите «Настройки» → «Связанные устройства». На Android этот раздел находится в меню ⋮.', 'Нажмите «Привязка устройства» и отсканируйте QR-код.', 'Дождитесь подтверждения подключения.'];
        const visual = el('div', { class: 'qr-visual' });
        if (qr.image && !qr.expired) visual.append(el('img', { class: 'connection-qr', src: qr.image, alt: `QR-код подключения ${CHANNEL_LABELS[channel]}`, referrerpolicy: 'no-referrer' }));
        else visual.append(el('div', { class: 'qr-placeholder', role: 'status' }, [waQrBlocked ? icon('warning', 28) : qr.expired ? icon('refresh', 28) : el('span', { class: 'connection-spinner' }), el('span', {}, waQrBlocked ? 'QR-код пока не получен' : qr.expired ? 'Обновляем QR-код…' : 'Получаем QR-код…')]));
        const qrStatus = el('p', { class: 'qr-status', role: 'status' }, waQrBlocked ? 'Повторите подключение или проверьте связь с сервером.' : qr.expired ? 'Предыдущий код больше не действует.' : qr.expiry ? '' : 'Подключение подтвердится автоматически.');
        if (qr.expiry && !qr.expired) qrStatus.textContent = `Код действует ещё ${Math.max(0, Math.ceil((qr.expiry.getTime() - Date.now()) / 1000))} с`;
        visual.append(qrStatus);
        stage.append(el('div', { class: 'qr-layout' }, [el('div', {}, [el('h3', { class: 'connection-subtitle' }, 'Свяжите ваш аккаунт с CRM'), el('ol', { class: 'connection-steps' }, steps.map(text => el('li', {}, text))), el('p', { class: 'connection-help' }, telegram ? 'При включённой двухэтапной проверке Telegram дополнительно запросит пароль.' : 'Клиенты увидят обычный чат с вашим рабочим номером.')]), visual]));
        if (account) stage.append(el('div', { class: 'qr-actions' }, [button(waQrBlocked || ['new', 'created'].includes(account.state) ? 'Повторить подключение' : 'Обновить QR-код', () => runAction(telegram ? 'qr' : 'login'), '', 'refresh'), telegram ? button('Войти по номеру телефона', () => { authMethod = 'phone'; phoneEntry = true; showError(''); render(true); }, 'subtle') : null]));
        if (!telegram) stage.append(el('p', { class: 'connection-caution' }, 'Подключение через связанные устройства использует неофициальную интеграцию. WhatsApp может ограничить её работу.'));
      } else {
        stage.append(el('div', { class: 'connection-unavailable' }, [el('div', { class: 'setup-illustration' }, icon(disconnected ? 'check' : 'refresh', 28)), el('h3', {}, disconnected ? 'Аккаунт отключён' : 'Нужно повторить подключение'), el('p', {}, disconnected ? 'История переписки сохранена в CRM.' : 'Откройте приложение на телефоне и начните вход ещё раз.'), button('Подключить снова', () => { disconnected = false; if (telegram) { authMethod = 'phone'; phoneEntry = true; showError(''); render(true); } else runAction('login'); }, 'primary')]));
      }
      if (!telegram) {
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
      if (!busy) render();
    };
    const expiryTimer = setInterval(() => {
      if (!alive() || busy || phase !== 'qr') return;
      if (!telegram) { render(); return; }
      const qr = qrDetails();
      if (qr.expired) render();
      else if (qr.expiry) { const node = stage.querySelector('.qr-status'); if (node) node.textContent = `Код действует ещё ${Math.max(0, Math.ceil((qr.expiry.getTime() - Date.now()) / 1000))} с`; }
    }, 1000);
    this._modalCleanup = () => { clearInterval(expiryTimer); for (const input of content.querySelectorAll('input')) input.value = ''; };
    render(true);
    if (account && this._connectionJobs.has(account.id)) runAction(this._connectionJobs.get(account.id).action);
    else if ((!originalAccount && !telegram) || autoLogin) runAction('login');
  }
  openManageAccount(originalAccount, autoLogin = false) {
    if (['telegram', 'whatsapp_web'].includes(originalAccount.channel)) { this.openGuidedConnection(originalAccount.channel, originalAccount, autoLogin); return; }
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
    try {
      for (let attempt = 0; attempt < 90; attempt += 1) {
        if (generation !== this._generation || !this.isConnected || !isActive()) throw new Error('Сеанс завершён.');
        const job = await this.api(`/api/jobs/${encodeURIComponent(id)}`);
        if (!isActive() || generation !== this._generation) throw new Error('Сеанс завершён.');
        if (['done', 'completed', 'succeeded'].includes(job.status)) return job;
        if (['failed', 'error'].includes(job.status)) { const error = new Error(typeof job.error === 'string' ? job.error : job.error?.message || job.result?.error || 'Провайдер не выполнил запрос.'); error.jobFailed = true; throw error; }
        onUpdate(job.status);
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      throw new Error('Задача всё ещё выполняется. Проверьте состояние подключения позднее; новый запрос не требуется.');
    } finally { this._jobs.delete(key); }
  }
  openNewConversation() {
    const account = this.account;
    if (!account) { this.openConnect(); return; }
    const hints = {
      telegram: ['Username, телефон или ID Telegram', '@username или +79991234567', 'Для существующих диалогов используйте синхронизацию. Доступность нового получателя зависит от Telegram.'],
      whatsapp_cloud: ['Телефон клиента', '79991234567', 'Номер в международном формате. Если клиент ещё не писал компании или прошло 24 часа, выберите «Одобренный шаблон» в форме отправки.'],
      whatsapp_web: ['Телефон клиента', '79991234567', 'Рабочий номер должен быть подключён через связанные устройства.'],
      max_bot: ['ID чата MAX', '123456789', 'Бот может писать клиенту после того, как клиент сам начал диалог.'],
      max_personal: ['Телефон, пользователь или чат MAX', '+79991234567', 'Для первого контакта: +79991234567 или phone:+79991234567. Для известного пользователя: user:123456. Число без префикса — ID чата. Если номер скрыт или не найден, провайдер вернёт ошибку до отправки.'],
      sms_gateway: ['Телефон клиента', '+79991234567', 'SMS отправляется с SIM-карты подключённого Android-устройства.'],
      demo: ['Идентификатор тестового клиента', 'client-001', 'Внешние сообщения в демо-канале не отправляются.'],
    };
    const [peerLabel, placeholder, help] = hints[account.channel] || ['Получатель', '', 'Укажите идентификатор получателя.'];
    const content = this.openModal('Новый диалог', `Отправитель: ${account.label}`);
    const peer = this.field('peer', peerLabel, 'text', true, placeholder);
    const title = this.field('title', 'Имя клиента', 'text', false, 'Например, Александр Смирнов');
    const error = this.formError();
    const submit = el('button', { type: 'submit', class: 'button primary' }, 'Открыть диалог');
    content.append(el('form', { onsubmit: async event => {
      event.preventDefault(); submit.disabled = true; error.hidden = true;
      try {
        const conversation = await this.post('/api/conversations', { account_id: account.id, peer: peer.input.value.trim(), title: title.input.value.trim() || peer.input.value.trim() });
        this.closeModal();
        if (account.id !== this._selectedAccount) await this.selectAccount(account.id);
        this._conversations = [...this._conversations.filter(c => c.id !== conversation.id), conversation];
        this.renderConversations(); await this.selectConversation(conversation.id);
        this._composer?.focus();
      } catch (e) { error.textContent = e.message; error.hidden = false; submit.disabled = false; }
    } }, [peer.node, el('p', { class: 'provider-description' }, help), title.node, error, el('div', { class: 'form-actions' }, [button('Отмена', () => this.closeModal()), submit])]));
    peer.input.focus();
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
