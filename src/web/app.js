const $runsList = document.getElementById('runs-list');
const $form = document.getElementById('run-form');
const $logs = document.getElementById('logs');
const $selectedRun = document.getElementById('selected-run');
const $autoscroll = document.getElementById('autoscroll');
const $copyLogs = document.getElementById('copy-logs');
const $health = document.getElementById('health-indicator');
const $tabButtons = document.querySelectorAll('.tab');
const $tabContents = document.querySelectorAll('.tabcontent');

const $outputFiles = document.getElementById('output-files');
const $outputView = document.getElementById('output-view');
const $outputName = document.getElementById('output-name');
const $refreshOutput = document.getElementById('refresh-output');

let selectedRunId = null;
const runOffsets = new Map(); // runId -> next log index

// Summary elements
const $sumDeleted = document.getElementById('sum-deleted');
const $sumReadOnly = document.getElementById('sum-readonly');
const $sumDrafts = document.getElementById('sum-drafts');

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
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
    if (data.lines && data.lines.length) {
      $logs.textContent += data.lines.join('\n') + '\n';
      if ($autoscroll.checked) {
        $logs.scrollTop = $logs.scrollHeight;
      }
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
      body: JSON.stringify({ email_address: email, app_password, email_limit }),
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
  runOffsets.set(runId, 0);
  Array.from($runsList.children).forEach(el => el.classList.toggle('active', el.dataset.runId === runId));
}

$copyLogs.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($logs.textContent);
  } catch {}
});

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

async function refreshSummary() {
  try {
    const data = await api('/summary');
    renderList($sumDeleted, data.deleted_emails, (it) => `${safe(it.subject)} — ${safe(it.sender)} <span class="meta">(${safe(it.reason) || ''})</span>`);
    renderList($sumReadOnly, data.read_only, (it) => `${safe(it.subject)} — ${safe(it.sender)} <span class="meta">[${safe(it.category)}/${safe(it.priority)}]</span>`);
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
