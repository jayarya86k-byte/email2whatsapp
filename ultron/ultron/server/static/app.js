/* Ultron dashboard client. No frameworks, no external requests. */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let token = localStorage.getItem('ultron_token') || '';
let busy = false;

async function api(path, body, method = 'POST') {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['X-Ultron-Token'] = token;
  const res = await fetch(path, {
    method: method === 'GET' ? 'GET' : (body ? method : 'GET'),
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------------- views ---------------- */
$$('nav button').forEach((btn) => btn.addEventListener('click', () => {
  $$('nav button').forEach((b) => b.classList.remove('active'));
  btn.classList.add('active');
  $$('.view').forEach((v) => v.classList.remove('active'));
  $(`#view-${btn.dataset.view}`).classList.add('active');
  refreshView(btn.dataset.view);
}));

function refreshView(view) {
  if (view === 'tasks') loadTasks();
  if (view === 'tools') loadTools();
  if (view === 'security') { loadPolicy(); loadVault(); }
  if (view === 'audit') loadAudit();
  if (view === 'settings') loadSettings();
}

/* ---------------- status ---------------- */
async function loadStatus() {
  try {
    const status = await api('/api/status', null, 'GET');
    const ok = status.model && status.model.ok;
    $('#status-dot').className = 'dot ' + (ok ? 'ok' : 'bad');
    $('#status-line').textContent =
      `${ok ? status.model.model : 'model offline'} · ${status.tools.length} tools · ` +
      `${status.memory.messages} messages · ${status.model.offline ? 'offline mode' : 'network allowed'}`;
    $('#settings-status').innerHTML = kv({
      'Agent': status.agent,
      'Model': status.model.model || '-',
      'Provider': `${status.model.provider} @ ${status.model.url}`,
      'Offline': status.model.offline_only ? 'enforced' : 'disabled',
      'Workspace': status.workspace,
      'Sandbox root': status.sandbox.sandbox_root,
      'Ask above risk': status.sandbox.ask_above_risk,
      'Deny at risk': status.sandbox.deny_at_risk,
      'Autonomous': status.autonomous ? 'yes' : 'no',
      'Audit chain': status.audit.ok ? `valid (${status.audit.records} records)` : 'BROKEN',
      'Dashboard clients': status.dashboard_clients,
      'Memory': JSON.stringify(status.memory),
    });
  } catch (err) {
    $('#status-dot').className = 'dot bad';
    $('#status-line').textContent = 'dashboard unreachable';
  }
}

function kv(obj) {
  return Object.entries(obj).map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join('');
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------------- chat ---------------- */
const stream = $('#stream');

function bubble(who, text, cls = '') {
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.innerHTML = `<div class="who">${esc(who)}</div><div class="body"></div>`;
  el.querySelector('.body').textContent = text;
  stream.appendChild(el);
  stream.scrollTop = stream.scrollHeight;
  return el;
}

function stepBlock(step) {
  const el = document.createElement('div');
  el.className = 'step';
  const args = JSON.stringify(step.args || {});
  const output = typeof step.result === 'string'
    ? step.result
    : JSON.stringify(step.result, null, 2);
  el.innerHTML = `<span class="tag ${esc(step.status)}">${esc(step.status)}</span>` +
    `<strong>${esc(step.action)}</strong> <span class="badge">risk ${step.risk ?? 0}</span>` +
    `<div class="muted">${esc(step.thought || '')}</div>` +
    (args !== '{}' ? `<div class="muted">args: ${esc(args.slice(0, 400))}</div>` : '') +
    (step.error ? `<div class="warn">${esc(step.error)}</div>` : '') +
    (output && output !== 'null' ? `<pre class="out">${esc(output.slice(0, 4000))}</pre>` : '');
  return el;
}

$('#composer').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  const input = $('#input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  bubble('you', text, 'user');
  busy = true;
  const holder = bubble('ultron', 'thinking…');
  try {
    const result = await api('/api/chat', {
      message: text,
      session: $('#session').value || 'default',
      use_tools: $('#use-tools').checked,
    });
    holder.querySelector('.body').textContent = result.answer || '(no answer)';
    (result.steps || []).forEach((s) => holder.appendChild(stepBlock(s)));
    if (result.pending_approval) showApproval(result.pending_approval);
    if ($('#speak').checked) speak(result.answer);
  } catch (err) {
    holder.classList.add('error');
    holder.querySelector('.body').textContent = err.message;
  } finally {
    busy = false;
    loadTasks();
  }
});

$('#input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('#composer').requestSubmit();
  }
});

/* ---------------- approvals ---------------- */
function showApproval(pending) {
  const box = $('#approval');
  box.classList.remove('hidden');
  box.innerHTML = `<strong>Approval required</strong> — ${esc(pending.reason || '')}` +
    `<div class="muted">${esc(pending.tool)} (${esc(pending.band)}, risk ${pending.risk})</div>` +
    `<pre class="out">${esc(JSON.stringify(pending.args, null, 2))}</pre>`;
  const row = document.createElement('div');
  row.className = 'row';
  const approve = document.createElement('button');
  approve.className = 'primary';
  approve.textContent = 'Approve once';
  const always = document.createElement('button');
  always.textContent = 'Approve for this session';
  const reject = document.createElement('button');
  reject.textContent = 'Reject';
  const decide = async (approveIt) => {
    await api('/api/approve', { approve: approveIt });
    box.classList.add('hidden');
  };
  approve.onclick = () => decide(true);
  always.onclick = () => decide(true);
  reject.onclick = () => decide(false);
  row.append(approve, always, reject);
  box.appendChild(row);
}

/* ---------------- SSE ---------------- */
function connect() {
  const source = new EventSource('/api/stream');
  for (const type of ['run_start', 'plan', 'approval_required', 'result']) {
    source.addEventListener(type, (event) => {
      const data = JSON.parse(event.data);
      if (type === 'approval_required' && data.pending && data.pending.tool) {
        showApproval(data.pending);
      }
      if (type === 'result' && data.result && data.result.pending_approval) {
        showApproval(data.result.pending_approval);
      }
      if (type === 'plan') renderPlan(data);
    });
  }
  source.onerror = () => { /* browser retries automatically */ };
}

function renderPlan(data) {
  const target = $('#plan');
  target.innerHTML = data.summary ? `<p class="muted">${esc(data.summary)}</p>` : '';
  const list = document.createElement('ol');
  (data.steps || []).forEach((step) => {
    const li = document.createElement('li');
    li.textContent = `${step.title}${step.needs_confirmation ? ' (confirm)' : ''}`;
    list.appendChild(li);
  });
  target.appendChild(list);
}

$('#plan-btn').addEventListener('click', async () => {
  const goal = $('#input').value.trim() || prompt('What should Ultron plan for?');
  if (!goal) return;
  $('#plan').innerHTML = '<p class="muted">planning…</p>';
  try {
    renderPlan(await api('/api/plan', { goal }));
  } catch (err) {
    $('#plan').innerHTML = `<p class="warn">${esc(err.message)}</p>`;
  }
});

/* ---------------- tasks ---------------- */
async function loadTasks() {
  const { tasks } = await api('/api/tasks', null, 'GET');
  $('#tasks').innerHTML = tasks.length ? tasks.map((t) => `
    <div class="card">
      <h4>${esc(t.title || t.goal)}</h4>
      <div class="meta"><span class="pill">${esc(t.status)}</span>${esc(t.id)} ·
        ${new Date((t.updated || 0) * 1000).toLocaleString()} ·
        ${(t.steps || []).length} steps</div>
      ${t.result ? `<p>${esc(String(t.result).slice(0, 600))}</p>` : ''}
    </div>`).join('') : '<p class="muted">No tasks yet.</p>';
}

/* ---------------- tools ---------------- */
async function loadTools() {
  const { tools } = await api('/api/tools', null, 'GET');
  $('#tools').innerHTML = tools.map((t) => {
    const level = t.risk >= 0.8 ? 'risk-high' : t.risk >= 0.5 ? 'risk-mid' : 'risk-low';
    const params = Object.entries(t.parameters.properties || {})
      .map(([k, v]) => `${k}:${v.type}`).join(' ');
    return `<div class="card">
      <h4>${esc(t.name)} <span class="pill ${level}">risk ${t.risk}</span>
        <span class="pill">${esc(t.capability)}</span></h4>
      <div class="muted">${esc(t.description)}</div>
      <div class="badge">${esc(params)}</div>
    </div>`;
  }).join('');
}

/* ---------------- policy ---------------- */
let policyOverrides = {};
async function loadPolicy() {
  const policy = await api('/api/policy', null, 'GET');
  policyOverrides = { ...policy.overrides };
  const known = ['run_shell', 'run_python', 'write_file', 'delete_file', 'create_pdf',
    'generate_challan', 'fs.delete', 'scaffold_project', 'gst_invoice', 'income_tax_compute'];
  $('#policy').innerHTML = known.map((name) => `
    <div class="row2"><span>${esc(name)}</span>
      <select data-tool="${esc(name)}">
        ${['', 'allow', 'ask', 'deny'].map((v) => `<option value="${v}">${v || 'default'}</option>`).join('')}
      </select></div>`).join('');
  $$('#policy select').forEach((sel) => {
    sel.value = policyOverrides[sel.dataset.tool] || '';
  });
}

$('#policy-save').addEventListener('click', async () => {
  const overrides = {};
  $$('#policy select').forEach((sel) => { if (sel.value) overrides[sel.dataset.tool] = sel.value; });
  policyOverrides = await api('/api/policy', { overrides }).then((r) => r.policy.overrides);
  $('#policy').insertAdjacentHTML('afterbegin',
    '<p class="muted">saved · all decisions are audited</p>');
});

/* ---------------- vault ---------------- */
async function loadVault() {
  const vault = await api('/api/vault', null, 'GET');
  const form = $('#vault-set');
  if (vault.unlocked) {
    form.classList.remove('hidden');
    $('#vault-status').textContent = `unlocked · ${vault.keys.length} secrets` +
      (vault.keyring ? ` · OS keystore: ${vault.keyring}` : ' · file-based (stdlib cipher)');
    $('#vault-list').innerHTML = vault.keys.map(
      (k) => `<li>${esc(k)} = ${esc(vault.values[k])}</li>`).join('');
  } else {
    form.classList.add('hidden');
    $('#vault-status').textContent = 'locked';
  }
}

$('#vault-unlock').addEventListener('submit', async (event) => {
  event.preventDefault();
  const password = $('#vault-pass').value;
  try {
    await api('/api/vault/unlock', { password, create: $('#vault-create').checked });
    $('#vault-pass').value = '';
    loadVault();
  } catch (err) {
    $('#vault-status').textContent = err.message;
  }
});

$('#vault-set').addEventListener('submit', async (event) => {
  event.preventDefault();
  await api('/api/vault/secret', { key: $('#vault-key').value, value: $('#vault-value').value });
  $('#vault-key').value = ''; $('#vault-value').value = '';
  loadVault();
});

/* ---------------- audit ---------------- */
async function loadAudit() {
  const { entries } = await api('/api/audit?limit=120', null, 'GET');
  const verify = await api('/api/audit/verify', null, 'GET');
  $('#audit-verify').innerHTML = verify.ok
    ? `<span class="pill risk-low">chain intact · ${verify.records} records</span>`
    : `<span class="pill risk-high">chain broken at ${verify.offset}</span>`;
  $('#audit').innerHTML = entries.slice().reverse().map((e) => {
    const level = e.risk >= 0.8 ? 'risk-high' : e.risk >= 0.5 ? 'risk-mid' : 'risk-low';
    return `<div class="card">
      <h4>${esc(e.action)} <span class="pill ${level}">${esc(e.status)}</span>
        <span class="pill">${esc(e.iso)}</span></h4>
      <div class="badge">${esc(JSON.stringify(e.detail).slice(0, 500))}</div>
      <div class="badge">digest ${esc((e.digest || '').slice(0, 16))}…</div>
    </div>`;
  }).join('');
}
$('#audit-refresh').addEventListener('click', loadAudit);

/* ---------------- memory ---------------- */
$('#fact-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  await api('/api/memory/fact', { key: $('#fact-key').value, value: $('#fact-value').value });
  $('#fact-key').value = ''; $('#fact-value').value = '';
  loadFacts();
});
async function loadFacts() {
  const { facts } = await api('/api/memory/facts', null, 'GET');
  $('#facts').innerHTML = facts.map((f) => `<li><strong>${esc(f.key)}</strong>: ${esc(f.value)}</li>`).join('');
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  const { stt, tts } = await api('/api/voice/backends', null, 'GET');
  $('#voice-backends').innerHTML = kv({
    'Speech to text': stt.length ? stt.join(', ') : 'none installed',
    'Text to speech': tts.length ? tts.join(', ') : 'none installed',
  });
}

/* ---------------- voice ---------------- */
const voiceBtn = $('#voice-btn');
let recorder = null, chunks = [], recording = false;

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream, { mimeType: pickMime() });
    chunks = [];
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.start();
    recording = true;
    voiceBtn.classList.add('rec');
    voiceBtn.textContent = '● stop';
  } catch (err) {
    bubble('system', `microphone unavailable: ${err.message}`, 'error');
  }
}

function pickMime() {
  const options = ['audio/webm', 'audio/ogg', 'audio/mp4'];
  return options.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
}

function stopRecording() {
  return new Promise((resolve) => {
    if (!recorder) return resolve();
    recorder.onstop = async () => {
      recorder.stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      recording = false;
      voiceBtn.classList.remove('rec');
      voiceBtn.textContent = '🎙 Voice';
      const form = new FormData();
      form.append('audio', blob, 'clip.webm');
      const headers = {};
      if (token) headers['X-Ultron-Token'] = token;
      const res = await fetch('/api/voice/stt', { method: 'POST', body: form, headers });
      const data = await res.json();
      if (data.ok) {
        $('#input').value = data.text;
        $('#composer').requestSubmit();
      } else {
        bubble('system', `transcription failed: ${data.error || 'unknown'}`, 'error');
      }
      resolve();
    };
    recorder.stop();
  });
}

voiceBtn.addEventListener('click', () => (recording ? stopRecording() : startRecording()));
document.addEventListener('keydown', (event) => {
  if (event.ctrlKey && event.shiftKey && event.code === 'Space') {
    event.preventDefault();
    recording ? stopRecording() : startRecording();
  }
});

async function speak(text) {
  if (!text) return;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['X-Ultron-Token'] = token;
    const res = await fetch('/api/voice/tts', {
      method: 'POST', headers, body: JSON.stringify({ text }),
    });
    if (!res.ok) return;
    const contentType = res.headers.get('content-type') || '';
    if (!contentType.includes('audio')) return;
    const { audio } = await res.json();
    const bytes = Uint8Array.from(atob(audio), (c) => c.charCodeAt(0));
    new Audio(URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))).play();
  } catch { /* speech is optional */ }
}

/* ---------------- boot ---------------- */
$('#auto-approve').addEventListener('change', (event) => {
  fetch('/api/auto-approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Ultron-Token': token } : {}) },
    body: JSON.stringify({ enabled: event.target.checked }),
  });
});

loadStatus();
loadFacts();
connect();
setInterval(loadStatus, 15000);
