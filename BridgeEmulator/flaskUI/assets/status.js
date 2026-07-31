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

// What a Hue model id means, for the entity list. The bridge picks the model
// from the capabilities Home Assistant reports.
const MODEL_LABELS = {
  LCT015: 'colour',
  LTW001: 'colour temperature',
  LWB010: 'dimmable',
  LOM001: 'on/off',
};

let state = null;
let socket = null;
let pollTimer = null;
let refreshInFlight = false;
let logLevel = 'INFO';
let entities = [];
let entitiesLoaded = false;
let entityLoadInFlight = false;
let entityError = null;
let entityFilter = '';

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
      + 'capabilities, so none of them became a lamp on their own. Tick the ones '
      + 'that really are lamps in the entity list below. Skipped: '
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
      + 'Tick the ones that are lamps in the entity list below to expose them as '
      + `on/off plugs. For example: ${(discovery.without_capabilities_sample || []).join(', ')}`;
  }
}

// --- Home Assistant entities ------------------------------------------------

async function loadEntities(refresh) {
  if (entityLoadInFlight) return;
  entityLoadInFlight = true;
  const button = el('entity-refresh');
  button.disabled = true;
  try {
    const result = await api(`/status/api/homeassistant/entities${refresh ? '?refresh=1' : ''}`);
    entities = result.entities || [];
    entityError = null;
  } catch (error) {
    // An attempt counts as loaded: retrying from every render would hammer a
    // bridge that cannot reach Home Assistant.
    entityError = error.message;
    if (refresh) toast('Could not read the entity list', error.message, 'error');
  } finally {
    entitiesLoaded = true;
    entityLoadInFlight = false;
    button.disabled = false;
    renderEntities();
  }
}

function capabilityLabel(entity) {
  if (entity.capable) return MODEL_LABELS[entity.modelid] || entity.modelid;
  // Nothing to infer a model from; exposing it anyway makes it an on/off plug.
  return entity.exposed ? 'on/off (no capabilities reported)' : 'no capabilities reported';
}

async function setEntityExposure(entity, expose) {
  const result = await api('/status/api/homeassistant/entities', {
    method: 'POST',
    body: JSON.stringify({ entity_id: entity.entity_id, expose }),
  });
  entities = result.entities || entities;
  state = result.state || state;
  renderEntities();
  render();
}

function entityRow(entity) {
  const row = document.createElement('label');
  row.className = 'entity';

  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.checked = Boolean(entity.exposed);
  toggle.addEventListener('change', async () => {
    toggle.disabled = true;
    const expose = toggle.checked;
    try {
      await setEntityExposure(entity, expose);
      toast(expose ? 'Exposed as a lamp' : 'Removed from the bridge',
            entity.name, expose ? 'success' : '');
    } catch (error) {
      toggle.checked = !expose;
      toast('Could not update the entity', error.message, 'error');
    } finally {
      toggle.disabled = false;
    }
  });
  row.appendChild(toggle);

  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = entity.name;
  row.appendChild(name);

  const id = document.createElement('span');
  id.className = 'id';
  id.textContent = entity.entity_id;
  row.appendChild(id);

  const caps = document.createElement('span');
  caps.className = entity.capable ? 'caps' : 'caps weak';
  caps.textContent = capabilityLabel(entity);
  caps.title = (entity.supported_color_modes || []).join(', ') || 'no supported_color_modes';
  row.appendChild(caps);

  const value = document.createElement('span');
  value.className = 'value';
  value.textContent = entity.state ?? '';
  row.appendChild(value);
  return row;
}

// Rendered only when the list itself changes - rebuilding a few hundred rows
// on every status event would throw away the scroll position mid-scroll.
function renderEntities() {
  const needle = entityFilter.trim().toLowerCase();
  const shown = entities.filter((entity) => !needle
    || entity.entity_id.toLowerCase().includes(needle)
    || (entity.name || '').toLowerCase().includes(needle));

  const exposed = entities.filter((entity) => entity.exposed).length;
  const summary = el('entity-summary');
  if (!entitiesLoaded) {
    summary.textContent = 'Loading…';
  } else if (entityError) {
    summary.textContent = `Could not read the entity list: ${entityError}`;
  } else if (!entities.length) {
    summary.textContent = 'Home Assistant has not reported any lights or switches yet. '
      + 'Connect it above, then reload.';
  } else {
    summary.textContent = `${entities.length} entities · ${exposed} exposed as lamps`
      + (needle ? ` · ${shown.length} match the filter` : '');
  }

  const list = el('entity-list');
  list.textContent = '';
  shown.forEach((entity) => list.appendChild(entityRow(entity)));
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
  // The entity list is fetched separately: it is far larger than the rest of
  // the state and does not belong in every poll.
  const haEnabled = Boolean((state.homeassistant || {}).enabled);
  el('ha-entities-panel').classList.toggle('hidden', !haEnabled);
  if (haEnabled && !entitiesLoaded) loadEntities(false);
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
      // A scan changes which entities are registered as lights.
      if (entitiesLoaded) loadEntities(false);
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
    // A different host or filter means a different entity list.
    entitiesLoaded = false;
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
    if (result.ok) await loadEntities(false);
  } catch (error) {
    toast('Test failed', error.message, 'error');
  } finally {
    button.disabled = false;
  }
});

el('entity-refresh').addEventListener('click', () => loadEntities(true));

el('entity-filter').addEventListener('input', (event) => {
  entityFilter = event.target.value;
  renderEntities();
});

el('log-debug').addEventListener('change', (event) => {
  logLevel = event.target.checked ? 'DEBUG' : 'INFO';
  refresh();
});

refresh().then(() => {
  fillHomeAssistantForm();
  connect();
});
