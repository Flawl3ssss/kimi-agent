/* Coomi · Kimi Code console client. Talks JSON-RPC over WebSocket using the ACP
   vocabulary exposed by kimi_agent/server.py; renders the normalized event
   stream and answers approvals/questions pushed by the DecisionBroker. */
'use strict';

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = {
  ws: null, seq: 0, sessionId: '', sessions: [], pending: [],
  attachments: [], running: false, reconnectIn: 0, config: {},
};

/* ── transport ───────────────────────────────────────────────────────── */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => {
    setNet('ok', 'соединение установлено');
    state.reconnectIn = 0;
  };
  state.ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'hello') return onHello(msg);
    if (msg.type === 'subscribed') return;
    if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) return resolve(msg);
    if (msg.type) onEvent(msg);
  };
  const retry = () => {
    setNet('bad', 'соединение потеряно — переподключение');
    state.reconnectIn = Math.min(15, (state.reconnectIn || 1) * 1.6);
    setTimeout(connect, state.reconnectIn * 1000);
  };
  state.ws.onclose = retry;
  state.ws.onerror = () => { try { state.ws.close(); } catch { retry(); } };
}

const awaiting = new Map();
let rpcId = 0;
function rpc(method, params = {}, timeout = 30000) {
  return new Promise((resolveP, rejectP) => {
    if (!state.ws || state.ws.readyState !== 1) return rejectP(new Error('нет соединения с сервером'));
    const id = ++rpcId;
    awaiting.set(id, { resolveP, rejectP });
    setTimeout(() => {
      if (awaiting.delete(id)) rejectP(new Error(`таймаут ${method}`));
    }, timeout);
    state.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
  });
}
function resolve(msg) {
  const hit = awaiting.get(msg.id);
  if (!hit) return;
  awaiting.delete(msg.id);
  if (msg.error) hit.rejectP(new Error(msg.error.message + (msg.error.data ? `: ${JSON.stringify(msg.error.data)}` : '')));
  else hit.resolveP(msg.result);
}

function onHello(msg) {
  if (msg.agent) $('#diag-agent').textContent = `${msg.agent.name} ${msg.agent.version}`;
  state.pending = msg.pending || [];
  renderDock();
  rpc('initialize', {}).then((info) => {
    setNet('ok', `Kimi Code ${info.agentInfo?.version ?? ''} готова`);
    $('#empty-cwd').textContent = info.settings?.workspace ?? '—';
    refreshSessions();
    loadTools();
  }).catch((err) => setNet('bad', err.message));
}

/* ── events ──────────────────────────────────────────────────────────── */
function onEvent(ev) {
  if (ev.seq) state.seq = Math.max(state.seq, ev.seq);
  const mine = !ev.session_id || ev.session_id === state.sessionId;
  switch (ev.type) {
    case 'turn_started':
      if (mine) { pushUser(ev.data.text || '(вложения)'); setRunning(true); signal('think'); }
      break;
    case 'thinking':
      if (mine) streamThinking(ev.data.text || '');
      break;
    case 'text':
      if (mine) {
        signal('tool');
        if (ev.data.image) addImage(ev.data.image, ev.data.caption, ev.data.path);
        else streamAgent(ev.data.text || '');
      }
      break;
    case 'tool_call':
      if (mine) { signal('tool'); upsertTool(ev.data, true); }
      break;
    case 'tool_update':
      if (mine) upsertTool(ev.data, false);
      break;
    case 'plan': renderPlan(ev.data.entries || []); break;
    case 'config': if (mine) renderConfig(ev.data.config_options || []); break;
    case 'commands': if (ev.data.commands) state.commands = ev.data.commands; break;
    case 'usage': if (mine) renderUsage(ev.data); break;
    case 'mode': if (mine) setSelect('#sel-mode', ev.data.mode); break;
    case 'session_info': if (mine && ev.data.title) renameSession(ev.session_id, ev.data.title); break;
    case 'approval_request': case 'question_request':
      upsertPending(ev.data, ev.type === 'question_request' ? 'q' : 'a');
      break;
    case 'approval_resolved': case 'question_resolved':
      dropPending(ev.data.id); break;
    case 'turn_completed':
      if (mine) { setRunning(false); signal('result'); closeStream(); finishTool(); toast('Ход завершён' + (ev.data.stop_reason && ev.data.stop_reason !== 'end_turn' ? `: ${ev.data.stop_reason}` : '')); }
      break;
    case 'turn_cancelled': if (mine) { setRunning(false); closeStream(); note('ход прерван'); } break;
    case 'turn_failed':
      if (mine) { setRunning(false); closeStream();
        note(`ошибка: ${ev.data.error}${ev.data.code === -32000 ? ' — не хватает авторизации, проверьте api_key в конфиге Kimi' : ''}`, 'err'); }
      break;
    case 'agent_exited':
      setNet('bad', `агент завершился (код ${ev.data.exit_code})`);
      (ev.data.stderr_tail || []).slice(-4).forEach((l) => note(l, 'err'));
      break;
    case 'agent_restarted': setNet('ok', 'агент перезапущен'); break;
    case 'info':
      if (ev.data.export) addExport(ev.data.export, ev.data.bytes);
      else if (ev.data.message && ev.data.message !== 'session created') note(ev.data.message + (ev.data.export ? '' : ''));
      break;
    case 'agent_log': break;
  }
  refreshSessions();
}

let signalTimer = null;
function signal(kind) {
  const strip = $('#signal');
  strip.classList.add('on');
  strip.querySelectorAll('.signal-node').forEach((n) => n.classList.toggle('live', n.dataset.k === kind));
  clearTimeout(signalTimer);
  signalTimer = setTimeout(() => {
    strip.classList.remove('on');
    strip.querySelectorAll('.signal-node').forEach((n) => n.classList.remove('live'));
  }, 9000);
}

function setRunning(on) {
  state.running = on;
  $('#btn-stop').disabled = !on;
  $('#btn-send').disabled = on;
  $('#foot-state').textContent = on ? 'агент работает…' : '';
}

/* ── transcript ──────────────────────────────────────────────────────── */
const thread = $('#thread');
const tools = new Map();
let agentBubble = null, thinkingBox = null;

function ensureThread() { const e = $('#empty'); if (e) e.remove(); }
function pushUser(text) {
  ensureThread();
  const wrap = el('div', 'msg user');
  wrap.append(el('div', 'who', 'вы'), el('div', 'bubble', text));
  thread.append(wrap); scroll();
}
function streamAgent(text) {
  ensureThread();
  if (!agentBubble) {
    const wrap = el('div', 'msg msg-agent');
    agentBubble = el('div', 'bubble');
    wrap.append(el('div', 'who', 'агент'), agentBubble);
    thread.append(wrap);
  }
  agentBubble.append(document.createTextNode(text));
  scroll();
}
function streamThinking(text) {
  ensureThread();
  if (!thinkingBox) {
    const details = el('details', 'thinking'); details.open = true;
    details.append(el('summary', null, 'размышление'));
    thinkingBox = el('div', 'thinking-body');
    details.append(thinkingBox);
    thread.insertBefore(details, thread.lastElementChild?.classList?.contains('msg-agent') ? thread.lastElementChild : null);
    if (!thread.contains(details)) thread.append(details);
  }
  thinkingBox.append(document.createTextNode(text));
  scroll();
}
function closeStream() {
  agentBubble = null;
  if (thinkingBox) { thinkingBox.closest('details').open = false; thinkingBox = null; }
}
function note(text, cls = 'note') {
  ensureThread();
  const node = el('div', cls === 'err' ? 'err' : 'note', text);
  thread.append(node); scroll();
}

function upsertTool(data, isNew) {
  let entry = tools.get(data.tool_call_id);
  if (!entry) {
    const card = el('div', 'tool'); card.dataset.open = 'false';
    const head = el('div', 'tool-head');
    const kind = el('span', `kind ${data.kind || ''}`, data.kind || 'шаг');
    const title = el('span', 'tool-title', data.title || data.tool_call_id);
    const pill = el('span', 'status-pill pending', 'ожидает');
    const body = el('div', 'tool-body');
    head.append(kind, title, pill);
    head.tabIndex = 0; head.setAttribute('role', 'button');
    head.addEventListener('click', () => { card.dataset.open = card.dataset.open === 'true' ? 'false' : 'true'; });
    head.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); head.click(); } });
    card.append(head, body);
    thread.append(card);
    entry = { card, kind, title, pill, body, input: null, terms: new Set(), renders: 0 };
    tools.set(data.tool_call_id, entry);
    scroll();
  }
  const { card, body } = entry;
  entry.title.textContent = data.title || entry.title.textContent;
  if (data.kind) entry.kind.textContent = data.kind, entry.kind.className = `kind ${data.kind}`;
  if (data.status) {
    entry.pill.textContent = data.status;
    entry.pill.className = `status-pill ${data.status}`;
  }
  const input = JSON.stringify(data.input ?? (data.content || []).find((c) => c.type === 'content')?.text ?? '', null, 0);
  if (input && input !== '""' && entry.input !== input) {
    entry.input = input;
    let pre = body.querySelector('pre.in');
    if (!pre) { pre = el('pre', 'in'); body.prepend(pre); }
    pre.textContent = prettify(input);
  }
  for (const c of data.content || []) {
    if (c.type === 'diff') renderDiff(body, c);
    else if (c.type === 'terminal' && c.terminal_id) fetchTerminal(data.tool_call_id, c.terminal_id, body, entry);
  }
  if (data.output && !body.querySelector('.out')) {
    const out = el('pre', 'out');
    out.textContent = typeof data.output === 'string' ? data.output : JSON.stringify(data.output, null, 1);
    body.append(el('div', 'who', 'результат'), out);
  }
  if (data.status === 'failed') card.dataset.open = 'true';
  if (data.status === 'completed' || data.status === 'failed') tools.set(data.tool_call_id, { ...entry, done: true });
}

function prettify(text) {
  try { return JSON.stringify(JSON.parse(text), null, 1); } catch { return String(text).slice(0, 4000); }
}
function renderDiff(body, c) {
  if (body.querySelector('[data-diff]')) return;
  const box = el('div', 'diff'); box.dataset.diff = c.path;
  (c.old_text || '').split('\n').forEach((l) => box.append(el('div', 'del', '-' + l)));
  c.new_text.split('\n').forEach((l) => box.append(el('div', 'add', '+' + l)));
  body.append(el('div', 'who', c.path), box);
}
async function fetchTerminal(toolCallId, termId, body, entry) {
  if (entry.terms.has(termId)) return;
  entry.terms.add(termId);
  const box = el('div', 'term-out');
  const pre = el('pre'); pre.textContent = '…';
  box.append(el('div', 'who', `терминал ${termId}`), pre);
  body.append(box);
  for (let i = 0; i < 40; i++) {
    try {
      const res = await rpc('coomi/raw', { method: 'terminal/output', params: { sessionId: state.sessionId, terminalId: termId } });
      if (res?.output !== undefined) { pre.textContent = res.output || '(без вывода)'; }
      if (res?.exit_status) { pre.textContent = `${res.output || ''}\n[exit ${res.exit_status.exit_code ?? res.exit_status.signal}]`; break; }
    } catch { break }
    if (entry.done && i > 2) break;
    await new Promise((r) => setTimeout(r, 700));
  }
}
function finishTool() { /* tool cards self-close via status pills */ }

function addImage(img, caption, path) {
  ensureThread();
  const fig = el('figure', 'imgcard');
  const node = el('img'); node.loading = 'lazy';
  node.src = img.uri || (path ? `/file?path=${encodeURIComponent(path)}` : `data:${img.mime_type};base64,${img.data}`);
  node.alt = caption || 'изображение от агента';
  fig.append(node);
  if (caption) fig.append(el('figcaption', null, caption));
  thread.append(fig); scroll();
}
function addExport(path, bytes) {
  ensureThread();
  const name = path.split('/').pop();
  const box = el('div', 'export');
  box.append(el('span', null, `файл готов: ${name}${bytes ? ` (${(bytes / 1024).toFixed(0)} КБ)` : ''}`));
  const link = el('a', null, 'сохранить'); link.href = `/file?path=${encodeURIComponent(path)}`; link.download = name;
  box.append(link);
  thread.append(box); scroll();
}
function scroll() { thread.scrollTop = thread.scrollHeight; }

/* ── left rail ───────────────────────────────────────────────────────── */
async function refreshSessions() {
  try {
    const res = await rpc('session/list', {}, 12000);
    state.sessions = res.live || [];
    renderSessions();
  } catch { /* keep the last view */ }
}
function renderSessions() {
  const list = $('#sessions');
  list.textContent = '';
  $('#sessions-hint').hidden = state.sessions.length > 0;
  state.sessions.slice(0, 24).forEach((s) => {
    const li = el('li');
    li.setAttribute('role', 'button'); li.tabIndex = 0;
    li.setAttribute('aria-current', String(s.id === state.sessionId));
    li.append(el('div', 'sess-top', ''), el('div', 'sess-meta',
      `${s.status === 'running' ? 'работает' : s.mode} · ${s.model || '—'}${s.usage?.used ? ` · ${Math.round(s.usage.used / 1000)}k ток.` : ''}`));
    li.firstChild.append(el('span', 'sess-title', s.title || s.id.replace('session_', '#').slice(0, 14)),
      el('span', 'sess-id', s.status === 'running' ? '●' : ''));
    li.onclick = () => switchSession(s.id);
    li.onkeydown = (e) => { if (e.key === 'Enter') switchSession(s.id); };
    list.append(li);
  });
}
function renameSession(id, title) {
  const s = state.sessions.find((x) => x.id === id);
  if (s) { s.title = title; renderSessions(); }
}
async function switchSession(id) {
  state.sessionId = id; state.seq = 0;
  tools.clear(); thread.textContent = ''; closeStream();
  $('#sid').textContent = id.replace('session_', '#').slice(0, 12);
  renderSessions();
  try {
    await rpc('coomi/raw', { method: 'session/resume', params: { sessionId: id, cwd: (state.sessions.find((s) => s.id === id) || {}).cwd || undefined } });
  } catch { /* resume is best-effort for live sessions */ }
  const snap = state.sessions.find((s) => s.id === id);
  if (snap) renderConfig(snap.config_options || []), renderPlan(snap.plan || []), snap.usage && renderUsage(snap.usage);
  const res = await rpc('session/events', { sessionId: id, limit: 400 }).catch(() => null);
  if (res) res.events.forEach(onEvent);
  $('#prompt').focus();
}

async function newSession() {
  const btn = $('#btn-new'); btn.disabled = true;
  try {
    const s = await rpc('session/new', { mode: $('#sel-mode').value || undefined });
    state.sessionId = s.id;
    $('#sid').textContent = s.id.replace('session_', '#').slice(0, 12);
    renderConfig(s.config_options || []);
    thread.textContent = ''; tools.clear();
    await refreshSessions();
    toast('сессия создана');
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
}

function setSelect(sel, value) { const n = $(sel); if (value) n.value = value; }
function renderConfig(options) {
  if (!options?.length) return;
  state.config = {};
  options.forEach((o) => {
    const target = o.id === 'mode' ? '#sel-mode' : o.id === 'model' ? '#sel-model' : o.id === 'thinking' ? '#sel-thinking' : null;
    if (!target) return;
    const node = $(target);
    node.textContent = '';
    (o.options || []).forEach((c) => {
      // Element.append() returns undefined, so chaining `.value =` onto it
      // threw and left every select after the first option empty.
      const opt = el('option', null, c.name || c.value);
      opt.value = c.value;
      node.append(opt);
    });
    node.disabled = !(o.options || []).length;
    setSelect(target, o.current_value);
    state.config[o.id] = o.options || [];
    node.onchange = async () => {
      try { await rpc('session/set_config_option', { sessionId: state.sessionId, configId: o.id, value: node.value }); }
      catch (err) { toast(`${o.id}: ${err.message}`, true); }
    };
  });
}
function renderUsage(u) {
  if (!u?.size) return;
  const pct = Math.min(100, (u.used / u.size) * 100);
  $('#meter').hidden = false;
  const fill = $('#meter-fill'); fill.style.width = pct.toFixed(1) + '%';
  fill.parentElement.className = 'meter-bar' + (pct > 90 ? ' over' : pct > 75 ? ' hot' : '');
  $('#meter-text').textContent = `${(u.used / 1000).toFixed(1)}k / ${(u.size / 1000).toFixed(0)}k · ${pct.toFixed(0)}%`;
}
function renderPlan(entries) {
  const list = $('#plan');
  list.textContent = '';
  $('#plan-hint').hidden = entries.length > 0;
  $('#plan-count').textContent = entries.length ? `${entries.filter((e) => e.status === 'completed').length}/${entries.length}` : '';
  entries.forEach((e) => {
    const li = el('li', `s-${e.status}`);
    li.append(el('span', 'tick'), el('b', null, e.content));
    list.append(li);
  });
}

/* ── dock ────────────────────────────────────────────────────────────── */
function upsertPending(data, kind) {
  if (!data?.id) return;
  if (state.pending.some((p) => p.id === data.id)) return;
  state.pending.push({ ...data, kind });
  renderDock();
  setNet('wait', kind === 'q' ? 'агент ждёт ответа' : 'нужно подтверждение');
}
function dropPending(id) {
  if (!id) return;
  state.pending = state.pending.filter((p) => p.id !== id);
  renderDock();
  if (!state.pending.length && state.running) setNet('ok', 'агент работает');
  else if (!state.pending.length) setNet('ok', 'готова');
}
function renderDock() {
  const dock = $('#dock');
  // #dock-hint lives *inside* #dock, so clearing textContent detaches it and a
  // later $('#dock-hint') is null -- which threw here and left the whole
  // approval dock (and the initialize handshake that follows) dead. Keep the
  // node and re-attach it instead of recreating markup.
  const hint = $('#dock-hint');
  dock.textContent = '';
  if (hint) dock.append(hint);
  const badge = $('#pending-count');
  badge.textContent = String(state.pending.length);
  badge.className = 'badge' + (state.pending.length ? '' : ' zero');
  if (hint) hint.hidden = state.pending.length > 0;
  state.pending.forEach((p) => dock.append(p.kind === 'q' ? questionCard(p) : approvalCard(p)));
}
function decide(id, body) {
  rpc('coomi/decide', { id, ...body }, 15000)
    .then(() => dropPending(id))
    .catch((err) => toast(err.message, true));
}
function approvalCard(p) {
  const card = el('div', 'card');
  // Kimi titles are the bare tool name ("Bash"); the server extracts the real
  // command into `detail`, so prefer it for the headline.
  const headline = p.detail || p.title || 'подтверждение';
  card.append(el('div', 'card-t', `${p.title && p.detail ? p.title + ' · ' : ''}${headline}`));
  if (p.content?.length) {
    const detail = el('pre', null, p.content.map((c) =>
      c.type === 'diff' ? `--- ${c.path}\n${(c.new_text || '').slice(0, 800)}`
        : (c.text || '').replace(/^Requesting approval to\s+/i, '').slice(0, 800))
      .join('\n').trim());
    card.append(detail);
  }
  const opts = el('div', 'opts');
  (p.options || []).forEach((o, i) => {
    const btn = el('button', 'opt' + (i === 0 ? ' prime' : '') + (String(o.kind).startsWith('reject') ? ' reject' : ''), o.name || o.option_id);
    btn.type = 'button';
    btn.onclick = () => decide(p.id, { behavior: String(o.kind).startsWith('reject') ? 'reject' : o.kind === 'allow_always' ? 'allow_session' : 'allow', option_id: o.option_id });
    opts.append(btn);
  });
  card.append(opts);
  if (p.auto_resolve_after) {
    const foot = el('div', 'card-sub mono', `авто-разрешение через ${p.auto_resolve_after}s`);
    card.append(foot);
    let left = p.auto_resolve_after;
    const timer = setInterval(() => {
      if (!document.body.contains(foot)) return clearInterval(timer);
      left -= 1; foot.textContent = `авто-разрешение через ${Math.max(0, left)}s`;
      if (left <= 0) clearInterval(timer);
    }, 1000);
  }
  return card;
}
function questionCard(p) {
  const card = el('div', 'card q');
  card.append(el('div', 'card-t', p.message || 'вопрос'));
  const qs = p.questions || [];
  const answers = {};
  qs.forEach((q, i) => {
    card.append(el('div', 'card-sub', q.description || q.question));
    const opts = el('div', 'opts');
    const chosen = () => (q.options || []).find((o) => o.value === answers[q.id]) ||
      (q.options || []).find((o) => o.label === answers[q.id]);
    (q.options || []).forEach((o) => {
      const btn = el('button', 'opt', o.label || String(o.value));
      btn.type = 'button';
      btn.onclick = () => { answers[q.id] = o.value ?? o.label; btn.classList.add('prime');
        [...opts.children].forEach((c) => { if (c !== btn) c.classList.remove('prime'); }); };
      opts.append(btn);
    });
    card.append(opts);
    if (i === qs.length - 1) {
      const send = el('button', 'opt prime', 'Отправить ответ');
      send.type = 'button';
      send.onclick = () => {
        const content = {};
        qs.forEach((qq) => { content[`q${qs.indexOf(qq)}`] = chosen(qq) ? (answers[qq.id] ?? '') : (answers[qq.id] ?? ''); });
        decide(p.id, { behavior: 'answer', content });
      };
      card.append(send);
    }
  });
  const skip = el('button', 'opt reject', 'Пропустить');
  skip.type = 'button'; skip.onclick = () => decide(p.id, { behavior: 'reject' });
  card.append(skip);
  return card;
}

/* ── composer ────────────────────────────────────────────────────────── */
async function send() {
  const text = $('#prompt').value.trim();
  if (!text && !state.attachments.length) return;
  if (!state.sessionId) { await newSession(); if (!state.sessionId) return; }
  const images = state.attachments.map((a) => ({ data: a.b64, mime_type: a.type }));
  state.attachments = []; renderAttachments();
  $('#prompt').value = ''; autoGrow();
  setRunning(true); signal('think');
  try {
    await rpc('session/prompt', { sessionId: state.sessionId, prompt: text, images, wait: false }, 30000);
  } catch (err) {
    setRunning(false); closeStream();
    note(`не отправлено: ${err.message}`, 'err');
  }
}
$('#composer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
$('#prompt').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
function autoGrow() {
  const t = $('#prompt'); t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 40 * 16) + 'px';
}
$('#prompt').addEventListener('input', autoGrow);

$('#img').addEventListener('change', async (e) => {
  for (const file of e.target.files) {
    if (file.size > 5 * 1024 * 1024) { toast(`${file.name}: больше 5 МБ`, true); continue; }
    const b64 = (await readAsDataURL(file)).split(',', 2)[1];
    state.attachments.push({ name: file.name, type: file.type, b64 });
  }
  e.target.value = ''; renderAttachments();
});
const readAsDataURL = (file) => new Promise((res) => {
  const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(file);
});
function renderAttachments() {
  const box = $('#attachments');
  box.textContent = ''; box.hidden = !state.attachments.length;
  state.attachments.forEach((a, i) => {
    const chip = el('div', 'chip');
    chip.append(el('span', null, a.name));
    const x = el('button', null, '×'); x.type = 'button';
    x.onclick = () => { state.attachments.splice(i, 1); renderAttachments(); };
    chip.append(x); box.append(chip);
  });
}

/* ── buttons / boot ──────────────────────────────────────────────────── */
$('#btn-new').onclick = newSession;
$('#btn-stop').onclick = () => state.sessionId && rpc('session/cancel', { sessionId: state.sessionId })
  .then(() => note('прерываю…')).catch((e) => toast(e.message, true));
$('#btn-log').onclick = () => rpc('coomi/agent/log', { lines: 60 })
  .then((r) => (r.lines || []).forEach((l) => note(l)))
  .catch((e) => toast(e.message, true));
$('#btn-prune').onclick = () => rpc('coomi/sessions/prune', { keep: 3 })
  .then((r) => toast(`закрыто: ${(r.closed || []).length}`)).catch((e) => toast(e.message, true));
document.querySelectorAll('.ask').forEach((b) => { b.onclick = () => { $('#prompt').value = b.dataset.ask; autoGrow(); $('#prompt').focus(); }; });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.running) $('#btn-stop').click();
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') send();
});

function setNet(kind, text) {
  $('#net-dot').className = 'dot' + (kind === 'ok' ? ' ok' : kind === 'wait' ? ' wait' : ' bad');
  $('#net-text').textContent = text;
}
let toastTimer;
function toast(text, bad) {
  const node = el('div', 'toast' + (bad ? ' bad' : ''), text);
  $('#toasts').append(node);
  setTimeout(() => node.remove(), bad ? 7000 : 3200);
}
async function loadTools() {
  try {
    const res = await rpc('coomi/tools/list', {}).catch(() => null);
    if (!res) { const t = await (await fetch('/api/tools')).json(); $('#diag-tools').textContent = `инструменты: ${t.count}`; return; }
    $('#diag-tools').textContent = `инструменты: ${(res.tools || []).length}`;
  } catch { $('#diag-tools').textContent = 'инструменты: недоступны'; }
}

connect();

/* ── provider settings ──────────────────────────────────────────────────
 * Kimi Code takes credentials only from config.toml (never from the shell), so
 * this form is the supported way to configure a provider on a phone. The key
 * field is write-only: the server reports has_key, never the value, and an
 * empty submission keeps what is already stored. */
let SETTINGS = null;

async function openSettings() {
  $('#sheet').hidden = false;
  $('#st-note').textContent = '';
  $('#sheet-state').textContent = 'Читаем config.toml…';
  try {
    const res = await fetch('/api/settings');
    SETTINGS = await res.json();
    fillSettings(SETTINGS);
  } catch (e) {
    $('#sheet-state').textContent = 'не удалось прочитать настройки: ' + e.message;
  }
}

function fillSettings(s) {
  const types = s.provider_types || ['openai', 'kimi', 'anthropic'];
  $('#st-type').innerHTML = types.map((t) =>
    `<option value="${t}"${t === (s.provider_type || 'openai') ? ' selected' : ''}>${t}</option>`).join('');
  const modes = ['manual', 'yolo', 'auto'];
  $('#st-perm').innerHTML = modes.map((m) =>
    `<option value="${m}"${m === (s.permission_mode || 'manual') ? ' selected' : ''}>${m}</option>`).join('');
  $('#st-provider').value = s.provider || 'custom';
  $('#st-base').value = s.base_url || '';
  $('#st-model').value = s.model || '';
  $('#st-ctx').value = s.max_context_size || 204800;
  $('#st-compact').value = s.compact_at_percent || 90;
  $('#st-think').checked = s.thinking_enabled !== false;
  $('#st-key').value = '';
  $('#st-key').placeholder = s.has_key ? '— ключ уже сохранён —' : '— required —';
  $('#sheet-state').innerHTML = s.configured
    ? `Активно: <b>${esc(s.default_model || '—')}</b> · ${esc(s.provider_type || '?')} · `
      + `${s.has_key ? 'ключ есть' : 'ключа нет'} · конфиг <code>${esc(s.path || '')}</code>`
    : (s.parse_error
      ? `config.toml не разбирается: ${esc(s.parse_error)}`
      : `Провайдер не настроен. Ключ обязателен — Kimi не читает его из окружения.`);
  $('#st-providers').innerHTML = (s.providers || []).length
    ? '<b>В конфиге:</b> ' + s.providers.map((p) =>
        `${esc(p.name)} (${esc(p.type || '?')}${p.oauth ? ', OAuth' : ''}${p.has_key ? ', key' : ', no key'})`
      ).join(' · ')
    : '';
}

const esc = (v) => String(v == null ? '' : v).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

$('#settings-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = {
    provider: $('#st-provider').value.trim() || 'custom',
    provider_type: $('#st-type').value,
    base_url: $('#st-base').value.trim(),
    model: $('#st-model').value.trim(),
    api_key: $('#st-key').value.trim(),
    max_context_size: Number($('#st-ctx').value) || 204800,
    compact_at_percent: Number($('#st-compact').value || 0),
    permission_mode: $('#st-perm').value,
    thinking_enabled: $('#st-think').checked,
    use_env_subtable: $('#st-env').checked,
    restart: true,
  };
  $('#st-note').textContent = 'сохраняем…';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.message || `HTTP ${res.status}`);
    $('#st-note').textContent = data.restarted ? 'сохранено, ядро перезапущено' : 'сохранено, но перезапуск не удался';
    fillSettings(data);
    toast(data.restarted ? 'Модель применена' : 'Сохранено — перезапустите агент', !data.restarted);
    setTimeout(() => { $('#sheet').hidden = true; connect(); }, 1200);
  } catch (e) {
    $('#st-note').textContent = '';
    toast('не сохранено: ' + e.message, true);
  }
});

$('#btn-settings').onclick = openSettings;
$('#sheet-close').onclick = () => { $('#sheet').hidden = true; };
$('#sheet').addEventListener('click', (e) => { if (e.target === $('#sheet')) $('#sheet').hidden = true; });
