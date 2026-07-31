// Status page client.
//
// Live updates arrive over a websocket. Reverse proxies that refuse to upgrade
// the connection are common, so the page falls back to polling rather than
// silently showing stale data.

const POLL_INTERVAL_MS = 2000;
const RECONNECT_DELAY_MS = 3000;
const TOAST_TIMEOUT_MS = 6000;

const el = (id) => document.getElementById(id);
const linkState = el('link-state');

let state = null;
let socket = null;
let pollTimer = null;
let refreshInFlight = false;
let logLevel = 'INFO';

async function api(path, options) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.error) detail = body.error;
    } catch (e) { /* not JSON, keep the status code */ }
    throw new Error(detail);
  }
  return response.json();
}

function toast(title, body, kind) {
  const node = document.createElement('div');
  node.className = `toast ${kind || ''}`;
  const titleNode = document.createElement('div');
  titleNode.className = 'title';
  titleNode.textContent = title;
  node.appendChild(titleNode);
  if (body) {
    const bodyNode = document.createElement('div');
    bodyNode.className = 'body';
    bodyNode.textContent = body;
    node.appendChild(bodyNode);
  }
  el('toasts').appendChild(node);
  setTimeout(() => node.remove(), TOAST_TIMEOUT_MS);
}

function setLink(mode, text) {
  linkState.className = `link-state ${mode}`;
  linkState.textContent = text;
}

async function startScan() {
  el('scan-button').disabled = true;
  try {
    await api('/status/api/scan', { method: 'POST' });
    toast('Scan started', 'Watching for new lights…');
  } catch (error) {
    toast('Could not start the scan', error.message, 'error');
    el('scan-button').disabled = false;
  }
  refresh();
}

// --- rendering -------------------------------------------------------------

function dot(kind) {
  const node = document.createElement('span');
  node.className = `dot ${kind}`;
  return node;
}

function renderBridge() {
  const bridge = state.bridge || {};
  el('bridge-summary').textContent =
    `${bridge.name || 'DiyHue'} · ${bridge.ipaddress || ''} · ${bridge.lights ?? 0} lights`;
}

function scanProtocolCard(name, entry) {
  const card = document.createElement('div');
  card.className = 'protocol';

  const row = document.createElement('div');
  row.className = 'row';
  const marker = { ok: 'ok', error: 'error', running: 'busy', disabled: '' }[entry.state] || '';
  row.appendChild(dot(marker));
  const label = document.createElement('span');
  label.className = 'name';
  label.textContent = name;
  row.appendChild(label);
  card.appendChild(row);

  const detail = document.createElement('div');
  detail.className = entry.state === 'error' ? 'detail error' : 'detail';
  if (entry.state === 'error') detail.textContent = entry.error;
  else if (entry.state === 'ok') detail.textContent = `${entry.found} found`;
  else if (entry.state === 'running') detail.textContent = 'searching…';
  else detail.textContent = 'disabled';
  card.appendChild(detail);
  return card;
}

function renderScan() {
  const scan = state.scan || {};
  const running = scan.state === 'active';
  el('scan-button').disabled = running;

  const summary = el('scan-summary');
  if (running) {
    summary.textContent = 'Scanning…';
  } else if (scan.error) {
    summary.textContent = `Last scan failed: ${scan.error}`;
  } else if (scan.lastscan) {
    const found = (scan.found || []).length;
    summary.textContent = `Last scan finished ${scan.lastscan} · ${found} new light${found === 1 ? '' : 's'}`;
  } else {
    summary.textContent = 'No scan has run since the bridge started. '
      + 'Lights are only added to the bridge by a scan.';
  }

  const progress = el('scan-progress');
  const sweep = scan.sweep;
  if (running && sweep && sweep.total) {
    progress.classList.remove('hidden');
    progress.firstElementChild.style.width = `${Math.round((sweep.scanned / sweep.total) * 100)}%`;
  } else {
    progress.classList.add('hidden');
  }

  const protocols = el('scan-protocols');
  protocols.textContent = '';
  Object.entries(scan.protocols || {})
    .filter(([, entry]) => entry.state !== 'disabled')
    .forEach(([name, entry]) => protocols.appendChild(scanProtocolCard(name, entry)));

  const found = el('scan-found');
  found.textContent = '';
  (scan.found || []).forEach((light) => {
    const row = document.createElement('div');
    row.className = 'found';
    row.appendChild(dot('ok'));
    const id = document.createElement('span');
    id.className = 'id';
    id.textContent = `#${light.id}`;
    row.appendChild(id);
    const name = document.createElement('span');
    name.textContent = `${light.name} (${light.protocol})`;
    row.appendChild(name);
    found.appendChild(row);
  });
}

function metric(value, label) {
  const node = document.createElement('div');
  node.className = 'metric';
  const valueNode = document.createElement('span');
  valueNode.className = 'value';
  valueNode.textContent = value;
  const labelNode = document.createElement('span');
  labelNode.className = 'label';
  labelNode.textContent = label;
  node.append(valueNode, labelNode);
  return node;
}

function renderHomeAssistant() {
  const ha = state.homeassistant || {};
  const discovery = ha.discovery || {};
  el('ha-enabled').checked = Boolean(ha.enabled);

  const health = el('ha-health');
  health.textContent = '';
  const connection = document.createElement('div');
  connection.className = 'metric';
  const value = document.createElement('span');
  value.className = 'value';
  value.style.display = 'flex';
  value.style.alignItems = 'center';
  value.style.gap = '0.4rem';
  let marker = 'error';
  let text = 'disconnected';
  if (!ha.enabled) { marker = ''; text = 'disabled'; }
  else if (ha.authenticated) { marker = 'ok'; text = 'connected'; }
  else if (ha.connected) { marker = 'warn'; text = 'authenticating'; }
  value.append(dot(marker), document.createTextNode(text));
  const label = document.createElement('span');
  label.className = 'label url';
  label.textContent = ha.url || '';
  connection.append(value, label);
  health.appendChild(connection);
  health.appendChild(metric(discovery.entities_seen ?? 0, 'entities seen'));
  health.appendChild(metric(discovery.entities_included ?? 0, 'included'));
  health.appendChild(metric(discovery.entities_tagged ?? 0, 'tagged'));

  // Included is not the same as usable: switches and helpers pass the filter
  // but report no brightness or colour, so they can never be a Hue light.
  const incapable = discovery.entities_without_capabilities ?? 0;
  const usable = (discovery.entities_included ?? 0) - incapable;
  health.appendChild(metric(usable, 'usable as lights'));
  const registered = ((state.protocols || {}).homeassistant || {}).lights ?? 0;
  health.appendChild(metric(registered, 'registered as lights'));

  const diagnosis = el('ha-diagnosis');
  diagnosis.textContent = '';
  diagnosis.className = 'diagnosis hidden';
  if (ha.enabled && ha.last_error) {
    diagnosis.className = 'diagnosis error';
    diagnosis.textContent = ha.last_error;
    if (!ha.token_configured) {
      diagnosis.textContent += ' — no access token is configured.';
    }
  } else if (ha.enabled && discovery.entities_seen > 0 && discovery.entities_included === 0) {
    // This is the usual reason a scan reports nothing: nothing opted in.
    diagnosis.className = 'diagnosis';
    diagnosis.append(document.createTextNode(
      `${discovery.entities_seen} lights and switches were found but none are included. `
      + 'Tick "Include every light and switch by default", or add the attribute '));
    const code = document.createElement('code');
    code.textContent = 'diyhue: include';
    diagnosis.append(code, document.createTextNode(
      ` to the entities you want. Examples that were skipped: ${(discovery.excluded_sample || []).join(', ')}`));
  } else if (ha.enabled && discovery.entities_included > 0 && usable === 0) {
    // A stock Home Assistant carries many more switch-like helpers than lamps.
    diagnosis.className = 'diagnosis';
    diagnosis.textContent =
      `All ${discovery.entities_included} included entities report no light `
      + 'capabilities, so none of them can be exposed as a lamp. Skipped: '
      + `${(discovery.without_capabilities_sample || []).join(', ')}`;
  } else if (ha.enabled && usable > 0 && registered === 0) {
    // Passing the include filter only makes an entity a candidate. Nothing
    // reaches the light list until a discovery scan registers it.
    diagnosis.className = 'diagnosis';
    diagnosis.append(document.createTextNode(
      `${usable} entities can be exposed as lights, but none of them are `
      + 'registered yet. Home Assistant entities only appear in the app '
      + 'once a discovery scan has added them.'));
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'button primary inline';
    action.textContent = 'Scan for lights';
    action.addEventListener('click', () => startScan());
    diagnosis.append(action);
  } else if (ha.enabled && incapable > 0) {
    diagnosis.className = 'diagnosis info';
    diagnosis.textContent =
      `${incapable} included entities report no light capabilities and are not `
      + 'exposed as lamps - switches and helpers have no brightness or colour. '
      + `For example: ${(discovery.without_capabilities_sample || []).join(', ')}`;
  }
}

function integrationCard(name, entry) {
  const card = document.createElement('div');
  card.className = 'protocol';

  const row = document.createElement('div');
  row.className = 'row';
  const service = (state.services || {})[name] || {};
  let marker = '';
  if (entry.enabled && entry.managed) marker = service.running ? 'ok' : 'error';
  else if (entry.enabled) marker = 'ok';
  row.appendChild(dot(marker));

  const label = document.createElement('span');
  label.className = 'name';
  label.textContent = name;
  row.appendChild(label);

  const count = document.createElement('span');
  count.className = 'count';
  count.textContent = `${entry.lights ?? 0} lights`;
  count.title = 'Lights registered on this bridge by this integration';
  row.appendChild(count);

  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.checked = Boolean(entry.enabled);
  // hue and tradfri are switched on by pairing them, not by a flag.
  toggle.disabled = entry.toggleable === false;
  if (toggle.disabled) toggle.title = 'Switched on by pairing, not by a flag';
  toggle.addEventListener('change', async () => {
    toggle.disabled = true;
    try {
      state = await api(`/status/api/service/${name}`, {
        method: 'POST',
        body: JSON.stringify({ enabled: toggle.checked }),
      });
      render();
      toast(`${name} ${toggle.checked ? 'enabled' : 'disabled'}`, 'No restart needed.', 'success');
    } catch (error) {
      toggle.checked = !toggle.checked;
      toast(`Could not update ${name}`, error.message, 'error');
    } finally {
      toggle.disabled = false;
    }
  });
  row.appendChild(toggle);
  card.appendChild(row);

  const detail = document.createElement('div');
  detail.className = service.error ? 'detail error' : 'detail';
  if (service.error) detail.textContent = service.error;
  else if (entry.managed) detail.textContent = entry.enabled
    ? (service.running ? 'connected service running' : 'not running')
    : 'connection service, disabled';
  else if (entry.toggleable === false) detail.textContent = entry.enabled
    ? 'paired, used during scans'
    : 'not paired';
  else detail.textContent = 'used during scans';
  card.appendChild(detail);
  return card;
}

function renderIntegrations() {
  const container = el('integrations');
  container.textContent = '';
  Object.entries(state.protocols || {})
    .sort(([a, x], [b, y]) => Number(y.managed) - Number(x.managed) || a.localeCompare(b))
    .forEach(([name, entry]) => container.appendChild(integrationCard(name, entry)));
}

function renderLog() {
  const log = el('log');
  const wasAtBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = '';
  (state.log || []).forEach((entry) => {
    const line = document.createElement('span');
    line.className = entry.level;
    line.textContent = `${entry.time} ${entry.level.padEnd(8)} ${entry.name} ${entry.message}\n`;
    log.appendChild(line);
  });
  if (wasAtBottom) log.scrollTop = log.scrollHeight;
}

function render() {
  if (!state) return;
  renderBridge();
  renderScan();
  renderHomeAssistant();
  renderIntegrations();
  renderLog();
}

function fillHomeAssistantForm() {
  const ha = state.homeassistant || {};
  const form = el('ha-form');
  const url = new URL(ha.url ? ha.url.replace(/^ws/, 'http') : 'http://127.0.0.1:8123');
  if (!form.homeAssistantIp.value) form.homeAssistantIp.value = url.hostname;
  if (!form.homeAssistantPort.value) form.homeAssistantPort.value = url.port || 8123;
  form.homeAssistantUseHttps.checked = String(ha.url || '').startsWith('wss://');
  form.homeAssistantIncludeByDefault.checked = Boolean((ha.discovery || {}).include_by_default);
}

// --- transport -------------------------------------------------------------

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    state = await api(`/status/api/state?level=${logLevel}`);
    render();
  } catch (error) {
    setLink('', `offline: ${error.message}`);
  } finally {
    refreshInFlight = false;
  }
}

function startPolling() {
  if (pollTimer) return;
  setLink('polling', 'polling');
  pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function applyEvent(event) {
  switch (event.kind) {
    case 'snapshot':
      state = event.state;
      fillHomeAssistantForm();
      render();
      return;
    case 'ping':
      return;
    case 'light_found':
      toast('New light found', `${event.name} (${event.protocol})`, 'success');
      break;
    case 'light_error':
      toast('Light skipped', `${event.name}: ${event.error}`, 'error');
      break;
    case 'protocol_error':
      toast(`${event.protocol} discovery failed`, event.error, 'error');
      break;
    case 'scan_finished':
      toast('Scan finished', `${event.found} new light${event.found === 1 ? '' : 's'}`,
            event.found ? 'success' : '');
      break;
    case 'scan_progress':
      if (state && state.scan) {
        state.scan.sweep = { port: event.port, scanned: event.scanned, total: event.total };
        renderScan();
      }
      return;
    default:
      break;
  }
  // Every other event changes state we do not track incrementally.
  refresh();
}

function connect() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${window.location.host}/status/ws`);

  socket.addEventListener('open', () => {
    stopPolling();
    setLink('live', 'live');
  });
  socket.addEventListener('message', (message) => {
    try {
      applyEvent(JSON.parse(message.data));
    } catch (error) {
      console.error('malformed status event', error);
    }
  });
  socket.addEventListener('close', () => {
    socket = null;
    // Keep the page useful even where the proxy will not upgrade.
    startPolling();
    setTimeout(connect, RECONNECT_DELAY_MS);
  });
  socket.addEventListener('error', () => socket && socket.close());
}

// --- wiring ----------------------------------------------------------------

el('scan-button').addEventListener('click', () => startScan());

el('ha-enabled').addEventListener('change', async (event) => {
  try {
    state = await api('/status/api/service/homeassistant', {
      method: 'POST',
      body: JSON.stringify({ enabled: event.target.checked }),
    });
    render();
  } catch (error) {
    toast('Could not update Home Assistant', error.message, 'error');
  }
});

el('ha-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const payload = {
    homeAssistantIp: form.homeAssistantIp.value.trim(),
    homeAssistantPort: Number(form.homeAssistantPort.value) || 8123,
    homeAssistantUseHttps: form.homeAssistantUseHttps.checked,
    homeAssistantIncludeByDefault: form.homeAssistantIncludeByDefault.checked,
  };
  // An empty token field means "keep the stored one".
  if (form.homeAssistantToken.value) payload.homeAssistantToken = form.homeAssistantToken.value;
  try {
    state = await api('/status/api/homeassistant', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    form.homeAssistantToken.value = '';
    render();
    toast('Home Assistant settings saved', 'Reconnecting…', 'success');
  } catch (error) {
    toast('Could not save the settings', error.message, 'error');
  }
});

el('ha-test').addEventListener('click', async () => {
  const button = el('ha-test');
  button.disabled = true;
  try {
    const result = await api('/status/api/homeassistant/test', { method: 'POST' });
    const discovery = (result.status || {}).discovery || {};
    if (result.ok) {
      toast('Home Assistant reachable',
            `${discovery.entities_seen} entities, ${discovery.entities_included} included`,
            'success');
    } else {
      toast('Home Assistant unreachable', result.error, 'error');
    }
    await refresh();
  } catch (error) {
    toast('Test failed', error.message, 'error');
  } finally {
    button.disabled = false;
  }
});

el('log-debug').addEventListener('change', (event) => {
  logLevel = event.target.checked ? 'DEBUG' : 'INFO';
  refresh();
});

refresh().then(() => {
  fillHomeAssistantForm();
  connect();
});
