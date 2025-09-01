const $runsList = document.getElementById('runs-list');
const $form = document.getElementById('run-form');
const $logs = document.getElementById('logs');
const $selectedRun = document.getElementById('selected-run');
const $autoscroll = document.getElementById('autoscroll');
const $copyLogs = document.getElementById('copy-logs');
const $clearLogs = document.getElementById('clear-logs');
const $logFilter = document.getElementById('log-filter');
const $wrapLogs = document.getElementById('wrap-logs');
const $pauseLogs = document.getElementById('pause-logs');
const $health = document.getElementById('health-indicator');
const $tabButtons = document.querySelectorAll('.tab');
const $tabContents = document.querySelectorAll('.tabcontent');

const $outputFiles = document.getElementById('output-files');
const $outputView = document.getElementById('output-view');
const $outputName = document.getElementById('output-name');
const $refreshOutput = document.getElementById('refresh-output');
const $btnSignin = document.getElementById('btn-signin');
const $btnSignout = document.getElementById('btn-signout');
const $userEmail = document.getElementById('user-email');
const $btnSaveSettings = document.getElementById('btn-save-settings');
const $settingsEmail = document.getElementById('settings-email');
const $settingsAppPassword = document.getElementById('settings-app-password');
const $settingsSignatureName = document.getElementById('settings-signature-name');
const $settingsSignature = document.getElementById('settings-signature');
const $useSaved = document.getElementById('use-saved');
const $savedIndicator = document.getElementById('saved-indicator');

let selectedRunId = null;
const runOffsets = new Map(); // runId -> next log index
let logsBuffer = []; // in-memory lines for current run
let logFilterRegex = null;
let logsPaused = false;

// Summary elements
const $sumDeleted = document.getElementById('sum-deleted');
const $sumNotDeleted = document.getElementById('sum-notdeleted');
const $sumDrafts = document.getElementById('sum-drafts');

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (window.getIdToken) {
    try {
      const t = await window.getIdToken();
      if (t) headers['Authorization'] = 'Bearer ' + t;
    } catch {}
  }
  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderRunItem(run) {
  const li = document.createElement('div');
  li.className = 'run-item';
  li.dataset.runId = run.id;
  li.innerHTML = `
    <div class="id"><code>${run.id.slice(0,8)}</code></div>
    <div class="meta">
      <div class="email">${run.email_address}</div>
      <div class="time">${new Date(run.started_at).toLocaleString()}</div>
    </div>
    <div class="state"><span class="status status-${run.status}">${run.status}</span></div>
  `;
  li.addEventListener('click', () => selectRun(run.id));
  return li;
}

async function refreshRuns() {
  try {
    const data = await api('/runs');
    $runsList.innerHTML = '';
    let active = 0, completed = 0, failed = 0;
    data.runs.forEach(run => {
      $runsList.appendChild(renderRunItem(run));
      if (run.status === 'running') active++;
      if (run.status === 'completed') completed++;
      if (run.status === 'failed') failed++;
      // initialize offset for new runs
      if (!runOffsets.has(run.id)) runOffsets.set(run.id, 0);
    });
    document.getElementById('stat-runs').textContent = String(data.runs.length);
    document.getElementById('stat-active').textContent = String(active);
    document.getElementById('stat-completed').textContent = String(completed);
    document.getElementById('stat-failed').textContent = String(failed);

    if (!selectedRunId && data.runs.length) selectRun(data.runs[0].id);
  } catch (e) {
    console.error('Failed to fetch runs', e);
  }
}

async function tailLogs() {
  if (!selectedRunId) return;
  const nextIdx = runOffsets.get(selectedRunId) || 0;
  try {
    const data = await api(`/runs/${selectedRunId}/logs?start=${nextIdx}`);
    if (data.status === 'unknown') {
      toast('Run not found (server may have restarted)');
      logsPaused = true;
      return;
    }
    if (data.lines && data.lines.length) {
      // update buffer first
      logsBuffer.push(...data.lines);
      renderLogs();
      runOffsets.set(selectedRunId, data.next);
    }
    // update sidebar status color
    const item = Array.from($runsList.children).find(el => el.dataset.runId === selectedRunId);
    if (item) {
      const st = item.querySelector('.status');
      st.textContent = data.status;
      st.className = `status status-${data.status}`;
    }
  } catch (e) {
    // ignore transient errors
  }
}

$form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('email').value.trim();
  const app_password = document.getElementById('app_password').value.trim();
  const email_limit = parseInt(document.getElementById('email_limit').value || '5', 10);
  try {
    const res = await api('/runs', {
      method: 'POST',
      body: JSON.stringify($useSaved.checked ? { email_limit } : { email_address: email, app_password, email_limit }),
    });
    await refreshRuns();
    selectRun(res.run_id);
    // clear sensitive field
    document.getElementById('app_password').value = '';
  } catch (err) {
    alert('Failed to start run: ' + err.message);
  }
});

function selectRun(runId) {
  selectedRunId = runId;
  $selectedRun.textContent = runId;
  $logs.textContent = '';
  logsBuffer = [];
  runOffsets.set(runId, 0);
  Array.from($runsList.children).forEach(el => el.classList.toggle('active', el.dataset.runId === runId));
}

$copyLogs.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($logs.textContent);
  } catch {}
});

$clearLogs.addEventListener('click', () => { logsBuffer = []; renderLogs(true); });
$logFilter.addEventListener('input', () => {
  const v = $logFilter.value.trim();
  try { logFilterRegex = v ? new RegExp(v, 'i') : null; } catch { logFilterRegex = null; }
  renderLogs(true);
});
$wrapLogs.addEventListener('change', () => {
  $logs.style.whiteSpace = $wrapLogs.checked ? 'pre-wrap' : 'pre';
});
$pauseLogs.addEventListener('change', () => { logsPaused = $pauseLogs.checked; });

$tabButtons.forEach(btn => btn.addEventListener('click', () => {
  $tabButtons.forEach(b => b.classList.remove('active'));
  $tabContents.forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
}));

async function refreshOutputs() {
  try {
    const data = await api('/output');
    $outputFiles.innerHTML = '';
    data.files.forEach(f => {
      const li = document.createElement('li');
      li.innerHTML = `<button class="file">${f.name}</button><span class="meta">${new Date(f.modified).toLocaleTimeString()} • ${f.size}B</span>`;
      li.querySelector('button').addEventListener('click', () => loadOutputFile(f.name));
      $outputFiles.appendChild(li);
    });
  } catch (e) {
    // ignore
  }
}

async function loadOutputFile(name) {
  try {
    const data = await api(`/output/${encodeURIComponent(name)}`);
    $outputName.textContent = name;
    const content = data.content ?? data.raw ?? {};
    $outputView.textContent = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
  } catch (e) {
    $outputName.textContent = name;
    $outputView.textContent = 'Failed to load file';
  }
}

$refreshOutput.addEventListener('click', refreshOutputs);

async function refreshHealth() {
  try {
    const h = await fetch('/health');
    if (h.ok) {
      $health.textContent = 'Healthy';
      $health.className = 'health ok';
    } else {
      $health.textContent = 'Degraded';
      $health.className = 'health warn';
    }
  } catch {
    $health.textContent = 'Down';
    $health.className = 'health err';
  }
}

// Check auth config and disable Sign In if missing
async function checkAuthConfig() {
  try {
    const res = await fetch('/api/firebase-config');
    if (!res.ok) throw new Error('no config');
    const cfg = await res.json();
    const ok = !!cfg.apiKey;
    if (!ok) {
      $btnSignin.disabled = true;
      $userEmail.textContent = 'Auth config missing';
    } else {
      $btnSignin.disabled = false;
      if ($userEmail.textContent === 'Auth config missing') $userEmail.textContent = 'Not signed in';
    }
  } catch {
    $btnSignin.disabled = true;
    $userEmail.textContent = 'Auth config missing';
  }
}

// periodic refresh & tail
setInterval(refreshRuns, 3000);
setInterval(tailLogs, 800);
setInterval(refreshOutputs, 5000);
setInterval(refreshHealth, 5000);
setInterval(refreshSummary, 4000);

// initial load
refreshRuns();
refreshOutputs();
refreshHealth();
refreshSummary();
checkAuthConfig();

// Auth bindings
document.addEventListener('auth-changed', async (e) => {
  const user = e.detail.user;
  if (user) {
    $userEmail.textContent = user.displayName ? `${user.displayName} (${user.email})` : user.email || 'Signed in';
    $btnSignin.style.display = 'none';
    $btnSignout.style.display = '';
    // Pre-fill email fields
    document.getElementById('email').value = user.email || '';
    // Fetch and apply saved settings, prioritizing them over Firebase defaults if they exist
    try {
      const s = await api('/me/settings');
      if (s.email_address) {
        $settingsEmail.value = s.email_address;
      } else {
        $settingsEmail.value = user.email || ''; // Default to Firebase email if no saved email
      }
      if (s.signature_name) {
        $settingsSignatureName.value = s.signature_name;
      } else if (user.displayName) {
        $settingsSignatureName.value = user.displayName; // Default to Firebase display name
      }
      if (s.signature) {
        $settingsSignature.value = s.signature;
      } else if (user.displayName) {
        $settingsSignature.value = `Best regards,\n${user.displayName}`; // Default signature
      }
      $savedIndicator.textContent = s.has_secret ? 'Saved ✓' : 'No secret';
    } catch { 
      $savedIndicator.textContent = 'Unknown';
      // If fetching settings fails, still try to pre-fill with Firebase user data
      $settingsEmail.value = user.email || '';
      if (user.displayName) {
        $settingsSignatureName.value = user.displayName;
        $settingsSignature.value = `Best regards,\n${user.displayName}`;
      }
    }
  } else {
    $userEmail.textContent = 'Not signed in';
    $btnSignin.style.display = '';
    $btnSignout.style.display = 'none';
    $settingsEmail.value = '';
    document.getElementById('email').value = ''; // Clear email field on logout
    $settingsSignatureName.value = '';
    $settingsSignature.value = '';
    $savedIndicator.textContent = 'Sign in';
  }
});

// Ask Firebase to emit current auth state after we registered the listener
if (window.emitAuthState) {
  window.emitAuthState();
} else {
  setTimeout(() => { window.emitAuthState && window.emitAuthState(); }, 500);
}

$btnSignin.addEventListener('click', async (e) => {
  e.preventDefault();
  if (!window.__AUTH_AVAILABLE) return toast('Auth not configured');
  try { await window.signInWithGoogle(); }
  catch (err) { toast(String(err?.message || 'Sign-in failed')); }
});
$btnSignout.addEventListener('click', () => window.signOutFirebase && window.signOutFirebase());

$btnSaveSettings.addEventListener('click', async () => {
  try {
    await api('/me/settings', {
      method: 'PUT',
      body: JSON.stringify({
        email_address: $settingsEmail.value.trim() || null,
        app_password: $settingsAppPassword.value.trim() || null,
        signature_name: $settingsSignatureName.value.trim() || null,
        signature: $settingsSignature.value,
        auth_type: 'app_password',
      }),
    });
    $settingsAppPassword.value = '';
    toast('Settings saved');
  } catch (e) {
    toast('Failed to save settings: ' + e.message);
  }
});

$useSaved.addEventListener('change', () => {
  const disabled = $useSaved.checked;
  document.getElementById('email').disabled = disabled;
  document.getElementById('app_password').disabled = disabled;
});

function toast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function renderLogs(force = false) {
  if (logsPaused && !force) return;
  const filtered = logFilterRegex ? logsBuffer.filter(l => logFilterRegex.test(l)) : logsBuffer;
  $logs.textContent = filtered.join('\n') + (filtered.length ? '\n' : '');
  if ($autoscroll.checked) $logs.scrollTop = $logs.scrollHeight;
}

async function refreshSummary() {
  try {
    const data = await api('/summary');
    renderList($sumDeleted, data.deleted, (it) => `${safe(it.subject)} — ${safe(it.sender)} <span class="meta">(${safe(it.reason) || ''})</span>`);
    renderList($sumNotDeleted, data.not_deleted, (it) => `${safe(it.subject)} — ${safe(it.sender)} <span class="meta">[${safe(it.category) || ''}/${safe(it.priority) || ''}] ${safe(it.reason) || ''}</span>`);
    renderList($sumDrafts, data.drafts, (it) => `${safe(it.subject)} → ${safe(it.recipient)} <span class="meta">${safe(it.response_summary) || ''}</span>`);
  } catch (e) {
    // ignore
  }
}

function renderList(ul, items, fmt) {
  ul.innerHTML = '';
  if (!Array.isArray(items) || items.length === 0) {
    const li = document.createElement('li');
    li.textContent = 'None';
    li.className = 'muted';
    ul.appendChild(li);
    return;
  }
  items.forEach(it => {
    const li = document.createElement('li');
    li.innerHTML = fmt(it);
    ul.appendChild(li);
  });
}

function safe(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
