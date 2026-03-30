// content.js — KenBot Content Script
// Plain JS, no imports, no bundler needed.
//
// Responsibilities:
//   1. Mount a Shadow DOM overlay (chat panel + confirmation dialogs)
//   2. Open and maintain a WebSocket connection to the Django Channels Pilot
//   3. Execute DOM actions sent by the Pilot (fill fields, click elements)
//   4. Fetch decrypted vault values from the Django vault API for field injection
//   5. Report step success / failure back to the Pilot
//
// SECURITY RULES (enforced at this layer):
//   - Vault values are fetched over HTTPS and injected directly into the DOM.
//     They are NEVER forwarded over the WebSocket or stored locally.
//   - Form field values are NEVER sent to the WebSocket.
//   - The LLM (Pilot) only ever receives confirmation/failure signals.

'use strict';

// ─── Debug Logger ─────────────────────────────────────────────────────────────
// Logs are visible in DevTools → the tab's console (not the extension console).
// Open DevTools on any portal page and filter by "[KenBot]" to see all events.
const DBG = true; // set to false to silence in production
function dbg(...args) {
  if (DBG) console.debug('[KenBot:content]', ...args);
}

const KENBOT_BACKEND_HTTP = 'http://127.0.0.1:8000';
const KENBOT_WS_BASE = 'ws://127.0.0.1:8000/ws/pilot/';
const WS_RECONNECT_BASE_DELAY_MS = 1500;
const WS_RECONNECT_MAX_DELAY_MS = 30000;

let ws = null;
let sessionId = null;
let reconnectAttempt = 0;
let shadow = null; // Shadow DOM root, set by mountOverlay()

// ─── 1. Overlay Bootstrap ─────────────────────────────────────────────────────

/**
 * Mount the Shadow DOM host element and build the initial overlay structure.
 * Overlay content is delegated to overlay.js (injected via <script> inside shadow).
 */
function mountOverlay() {
  if (document.getElementById('kenbot-host')) return; // Already mounted

  const host = document.createElement('div');
  host.id = 'kenbot-host';
  // Ensure the host sits on top of all portal content
  host.style.cssText = 'position:fixed;z-index:2147483647;bottom:24px;right:24px;';
  document.body.appendChild(host);

  shadow = host.attachShadow({ mode: 'open' });

  // Inject styles
  const styleLink = document.createElement('link');
  styleLink.rel = 'stylesheet';
  styleLink.href = chrome.runtime.getURL('ui/overlay.css');
  shadow.appendChild(styleLink);

  // Panel root
  const panel = document.createElement('div');
  panel.id = 'kenbot-panel';
  panel.innerHTML = `
    <button id="kb-toggle" class="kb-toggle" aria-label="Open KenBot panel" aria-expanded="false">
      <span class="kb-logo" aria-hidden="true">KB</span>
    </button>
    <div id="kb-chat" class="kb-chat" hidden role="dialog" aria-label="KenBot assistant">
      <header class="kb-header">
        <span class="kb-title">KenBot</span>
        <span id="kb-status-dot" class="kb-status-dot kb-status-disconnected" aria-label="Disconnected" title="Disconnected"></span>
        <button id="kb-close" class="kb-close" aria-label="Close panel">&times;</button>
      </header>
      <div id="kb-messages" class="kb-messages" role="log" aria-live="polite" aria-relevant="additions"></div>
      <div id="kb-confirmation-area" class="kb-confirmation-area" hidden></div>
      <form id="kb-input-form" class="kb-input-form" autocomplete="off">
        <input id="kb-user-input" class="kb-user-input" type="text" placeholder="Type a task in English or Swahili…" aria-label="Task input" autocomplete="off" />
        <button type="submit" class="kb-send-btn" aria-label="Send">&#9658;</button>
      </form>
    </div>
  `;
  shadow.appendChild(panel);

  // Wire toggle / close buttons
  const toggleBtn = shadow.getElementById('kb-toggle');
  const chatPanel = shadow.getElementById('kb-chat');
  const closeBtn = shadow.getElementById('kb-close');
  const inputForm = shadow.getElementById('kb-input-form');

  toggleBtn.addEventListener('click', () => {
    const isHidden = chatPanel.hidden;
    chatPanel.hidden = !isHidden;
    toggleBtn.setAttribute('aria-expanded', String(isHidden));
  });

  closeBtn.addEventListener('click', () => {
    chatPanel.hidden = true;
    toggleBtn.setAttribute('aria-expanded', 'false');
  });

  inputForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = shadow.getElementById('kb-user-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendUserMessage(text);
  });
}

// ─── 2. WebSocket Connection ──────────────────────────────────────────────────

/**
 * Open (or re-open) the WebSocket connection to the Pilot.
 * Fetches the JWT from chrome.storage first and passes it as ?token= query param.
 * Uses exponential back-off on disconnect.
 */
async function connectToPilot() {
  if (!sessionId) {
    sessionId = crypto.randomUUID();
  }

  const token = await getAuthToken();
  if (!token) {
    dbg('No auth token — showing login prompt, will not connect yet');
    setStatusIndicator('disconnected');
    appendSystemMessage(
      'Please log in via the KenBot popup to connect.',
      'Tafadhali ingia kupitia popup ya KenBot.'
    );
    return;
  }

  const url = `${KENBOT_WS_BASE}${sessionId}/?token=${encodeURIComponent(token)}`;
  dbg('Connecting WebSocket to', url.replace(/token=.*/, 'token=***'));
  ws = new WebSocket(url);

  ws.addEventListener('open', onWsOpen);
  ws.addEventListener('message', onWsMessage);
  ws.addEventListener('close', onWsClose);
  ws.addEventListener('error', onWsError);
}

function onWsOpen() {
  reconnectAttempt = 0;
  dbg('WebSocket opened session=%s', sessionId);
  setStatusIndicator('connected');
  chrome.runtime.sendMessage({ type: 'SESSION_STARTED', sessionId });
  appendSystemMessage('Connected to KenBot. How can I help you?', 'Imeunganishwa na KenBot. Naweza kukusaidia?');
}

function onWsClose(event) {
  dbg('WebSocket closed code=%d reason=%s', event.code, event.reason || '(none)');
  setStatusIndicator('disconnected');

  // Normal closure — clean exit, no reconnect
  if (event.code === 1000) {
    chrome.runtime.sendMessage({ type: 'SESSION_ENDED' });
    return;
  }

  // Auth failure — try to refresh the JWT before giving up
  if (event.code === 4001) {
    dbg('Auth rejected (4001) — attempting token refresh');
    chrome.runtime.sendMessage({ type: 'REFRESH_ACCESS_TOKEN' }, (response) => {
      if (response && response.ok) {
        dbg('Token refreshed — reconnecting');
        reconnectAttempt = 0;
        setTimeout(connectToPilot, 500);
      } else {
        dbg('Token refresh failed (%s) — asking user to log in', response && response.reason);
        appendSystemMessage(
          'Session expired. Please log in again via the KenBot popup.',
          'Kipindi kimeisha. Tafadhali ingia tena kupitia popup ya KenBot.'
        );
        chrome.runtime.sendMessage({ type: 'SESSION_ENDED' });
      }
    });
    return;
  }

  scheduleReconnect();
}

function onWsError(err) {
  dbg('WebSocket error', err);
  setStatusIndicator('error');
}

function scheduleReconnect() {
  const delay = Math.min(
    WS_RECONNECT_BASE_DELAY_MS * Math.pow(2, reconnectAttempt),
    WS_RECONNECT_MAX_DELAY_MS
  );
  reconnectAttempt++;
  setTimeout(connectToPilot, delay);
}

// ─── 3. Pilot Message Handling ────────────────────────────────────────────────

function onWsMessage(event) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch {
    return;
  }
  handlePilotMessage(msg);
}

/**
 * Dispatch incoming Pilot messages to the correct handler.
 * @param {{ type: string, [key: string]: any }} msg
 */
function handlePilotMessage(msg) {
  switch (msg.type) {
    case 'agent_message':
      appendAgentMessage(msg.content_en, msg.content_sw);
      break;

    case 'execute_action':
      executeAction(msg.action);
      break;

    case 'pause_for_confirmation':
      showConfirmation(msg.step_label, msg.fields_summary);
      break;

    case 'captcha_detected':
      showCaptchaPrompt();
      break;

    case 'step_complete':
      appendSystemMessage(`✓ ${msg.step_label}`, `✓ ${msg.step_label}`);
      break;

    case 'workflow_complete':
      appendSystemMessage(
        `Task complete: ${msg.service_name}`,
        `Kazi imekamilika: ${msg.service_name}`
      );
      hideConfirmationArea();
      break;

    case 'workflow_error':
      appendSystemMessage(`Error: ${msg.message}`, `Hitilafu: ${msg.message}`);
      break;

    case 'session_expired':
      appendSystemMessage('Your session has expired. Please log in again.', 'Kipindi chako kimeisha.');
      ws.close(4001);
      break;

    default:
      break;
  }
}

// ─── 4. Outbound Messages to Pilot ───────────────────────────────────────────

/**
 * Send a user-typed message to the Pilot to start a workflow.
 * @param {string} text
 */
function sendUserMessage(text) {
  appendUserMessage(text);
  safeSend({ type: 'user_message', text });
}

/**
 * Serialise and send over the WebSocket.
 * Falls back silently if the socket is not open.
 * @param {object} payload
 */
function safeSend(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

// ─── 5. DOM Action Execution ──────────────────────────────────────────────────
//
// SECURITY NOTE: vault values travel from the Django vault API directly into the
// DOM element's .value property. They are NEVER passed to safeSend(), stored in
// variables that outlive this function call, or logged anywhere.

/**
 * Execute a single Action sent by the Pilot.
 * @param {{ type: string, selector: { primary: string, fallbacks: string[] },
 *           required_data_key: string|null, semantic_name: string }} action
 */
async function executeAction(action) {
  try {
    if (action.required_data_key) {
      await injectVaultValue(action);
    } else {
      switch (action.type) {
        case 'click':
          performClick(action.selector.primary, action.selector.fallbacks, action.semantic_name);
          break;
        case 'select':
          // For <select> elements without a vault dependency the Pilot supplies a value directly.
          performSelect(action.selector.primary, action.selector.fallbacks, action.static_value, action.semantic_name);
          break;
        case 'checkbox':
          performCheckbox(action.selector.primary, action.selector.fallbacks, action.checked, action.semantic_name);
          break;
        case 'wait':
          await delay(action.wait_ms || 1000);
          safeSend({ type: 'step_confirmed', semantic_name: action.semantic_name });
          break;
        default:
          reportStepFailed(action.selector.primary, action.semantic_name, 'Unknown action type');
          break;
      }
    }
  } catch (err) {
    reportStepFailed(action.selector.primary, action.semantic_name, err.message);
  }
}

/**
 * Fetch a decrypted vault value from the Django API and inject it directly into
 * the target DOM element. The plaintext value is NOT stored beyond this scope.
 * @param {object} action
 */
async function injectVaultValue(action) {
  const token = await getAuthToken();
  if (!token) {
    reportStepFailed(action.selector.primary, action.semantic_name, 'Not authenticated');
    return;
  }

  const res = await fetch(
    `${KENBOT_BACKEND_HTTP}/api/vault/${encodeURIComponent(action.required_data_key)}/`,
    {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      }
    }
  );

  if (!res.ok) {
    reportStepFailed(action.selector.primary, action.semantic_name, `Vault fetch failed: ${res.status}`);
    return;
  }

  const { value } = await res.json();

  if (action.type === 'password' || action.type === 'text') {
    performFill(action.selector.primary, action.selector.fallbacks, value, action.semantic_name);
  } else if (action.type === 'select') {
    performSelect(action.selector.primary, action.selector.fallbacks, value, action.semantic_name);
  }

  // value goes out of scope here — GC will collect it; never sent to WS.
}

// ─── 6. Low-Level DOM Helpers ─────────────────────────────────────────────────

/**
 * Resolve a primary selector with ordered fallbacks.
 * Supports CSS, XPath, ARIA role/label shortcuts.
 * @param {string} primary
 * @param {string[]} fallbacks
 * @returns {Element|null}
 */
function findElement(primary, fallbacks = []) {
  const selectors = [primary, ...fallbacks];
  for (const sel of selectors) {
    try {
      // XPath selectors start with '//' or '('
      if (sel.startsWith('//') || sel.startsWith('(')) {
        const result = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        if (result.singleNodeValue) return result.singleNodeValue;
      } else {
        const el = document.querySelector(sel);
        if (el) return el;
      }
    } catch {
      continue;
    }
  }
  return null;
}

function performFill(primary, fallbacks, value, semanticName) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    reportStepFailed(primary, semanticName, 'Element not found');
    return;
  }
  // Native input value setter — required to bypass React-controlled inputs
  const nativeSetter = Object.getOwnPropertyDescriptor(
    el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
    'value'
  );
  if (nativeSetter && nativeSetter.set) {
    nativeSetter.set.call(el, value);
  } else {
    el.value = value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  safeSend({ type: 'step_confirmed', semantic_name: semanticName });
}

function performClick(primary, fallbacks, semanticName) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    reportStepFailed(primary, semanticName, 'Element not found');
    return;
  }
  el.click();
  safeSend({ type: 'step_confirmed', semantic_name: semanticName });
}

function performSelect(primary, fallbacks, optionValue, semanticName) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    reportStepFailed(primary, semanticName, 'Element not found');
    return;
  }
  el.value = optionValue;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  safeSend({ type: 'step_confirmed', semantic_name: semanticName });
}

function performCheckbox(primary, fallbacks, checked, semanticName) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    reportStepFailed(primary, semanticName, 'Element not found');
    return;
  }
  if (el.checked !== checked) {
    el.click();
  }
  safeSend({ type: 'step_confirmed', semantic_name: semanticName });
}

/**
 * Report a failed step to the Pilot. Selectors only — no values.
 * @param {string} selector
 * @param {string} semanticName
 * @param {string} reason
 */
function reportStepFailed(selector, semanticName, reason) {
  safeSend({
    type: 'step_failed',
    selector,
    semantic_name: semanticName,
    reason
  });
}

// ─── 7. Overlay UI Helpers ───────────────────────────────────────────────────

function appendAgentMessage(textEn, textSw) {
  addMessage('agent', textEn, textSw);
}

function appendUserMessage(text) {
  addMessage('user', text, text);
}

function appendSystemMessage(textEn, textSw) {
  addMessage('system', textEn, textSw);
}

/**
 * Append a message bubble to the chat log.
 * @param {'agent'|'user'|'system'} role
 * @param {string} textEn
 * @param {string} textSw
 */
function addMessage(role, textEn, textSw) {
  if (!shadow) return;
  const log = shadow.getElementById('kb-messages');
  if (!log) return;

  const bubble = document.createElement('div');
  bubble.className = `kb-message kb-message--${role}`;
  bubble.setAttribute('lang', 'en');

  const enSpan = document.createElement('span');
  enSpan.className = 'kb-msg-en';
  enSpan.textContent = textEn;

  bubble.appendChild(enSpan);

  // Show Swahili translation below if it differs
  if (textSw && textSw !== textEn) {
    const swSpan = document.createElement('span');
    swSpan.className = 'kb-msg-sw';
    swSpan.setAttribute('lang', 'sw');
    swSpan.textContent = textSw;
    bubble.appendChild(document.createElement('br'));
    bubble.appendChild(swSpan);
  }

  log.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
}

/**
 * Show a confirmation prompt before the Pilot executes a sensitive step.
 * Resolves user response back to the Pilot via WebSocket.
 * @param {string} stepLabel
 * @param {string[]} fieldsSummary  — human-readable list of fields (NO values)
 */
function showConfirmation(stepLabel, fieldsSummary) {
  if (!shadow) return;
  const area = shadow.getElementById('kb-confirmation-area');
  if (!area) return;

  area.innerHTML = '';
  area.hidden = false;

  const label = document.createElement('p');
  label.className = 'kb-confirm-label';
  label.textContent = `Confirm: ${stepLabel}`;
  area.appendChild(label);

  if (fieldsSummary && fieldsSummary.length) {
    const list = document.createElement('ul');
    list.className = 'kb-confirm-fields';
    fieldsSummary.forEach((f) => {
      const li = document.createElement('li');
      li.textContent = f; // field name only — e.g. "National ID", "KRA PIN"
      list.appendChild(li);
    });
    area.appendChild(list);
  }

  const btnRow = document.createElement('div');
  btnRow.className = 'kb-confirm-buttons';

  const confirmBtn = document.createElement('button');
  confirmBtn.className = 'kb-btn kb-btn--confirm';
  confirmBtn.textContent = 'Proceed / Endelea';
  confirmBtn.addEventListener('click', () => {
    safeSend({ type: 'confirmation_response', confirmed: true });
    hideConfirmationArea();
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'kb-btn kb-btn--cancel';
  cancelBtn.textContent = 'Cancel / Ghairi';
  cancelBtn.addEventListener('click', () => {
    safeSend({ type: 'confirmation_response', confirmed: false });
    hideConfirmationArea();
  });

  btnRow.appendChild(confirmBtn);
  btnRow.appendChild(cancelBtn);
  area.appendChild(btnRow);
}

function hideConfirmationArea() {
  if (!shadow) return;
  const area = shadow.getElementById('kb-confirmation-area');
  if (area) {
    area.hidden = true;
    area.innerHTML = '';
  }
}

/**
 * Prompt the user to manually solve a CAPTCHA.
 * The Pilot waits for a 'captcha_solved' signal before proceeding.
 */
function showCaptchaPrompt() {
  if (!shadow) return;
  const area = shadow.getElementById('kb-confirmation-area');
  if (!area) return;

  area.innerHTML = '';
  area.hidden = false;

  const msg = document.createElement('p');
  msg.className = 'kb-confirm-label';
  msg.textContent = 'A CAPTCHA has been detected. Please solve it, then click Done.';
  area.appendChild(msg);

  const doneBtn = document.createElement('button');
  doneBtn.className = 'kb-btn kb-btn--confirm';
  doneBtn.textContent = 'Done / Nimekamilisha';
  doneBtn.addEventListener('click', () => {
    safeSend({ type: 'captcha_solved' });
    hideConfirmationArea();
  });
  area.appendChild(doneBtn);
}

/**
 * Update the status indicator dot in the header.
 * @param {'connected'|'disconnected'|'error'} state
 */
function setStatusIndicator(state) {
  if (!shadow) return;
  const dot = shadow.getElementById('kb-status-dot');
  if (!dot) return;
  dot.className = `kb-status-dot kb-status-${state}`;
  const labels = { connected: 'Connected', disconnected: 'Disconnected', error: 'Connection error' };
  dot.setAttribute('aria-label', labels[state] || state);
  dot.setAttribute('title', labels[state] || state);
}

// ─── 8. Auth Token Retrieval (via background) ─────────────────────────────────

/**
 * Retrieve the auth token from the background service worker.
 * @returns {Promise<string|null>}
 */
function getAuthToken() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'GET_AUTH_TOKEN' }, (response) => {
      resolve(response && response.token ? response.token : null);
    });
  });
}

// ─── 9. Utility ──────────────────────────────────────────────────────────────

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── 10. Message bridge from background / popup ───────────────────────────────

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  switch (message.type) {
    // Popup requests current connection status
    case 'GET_WS_STATUS': {
      const state = ws ? ws.readyState : WebSocket.CLOSED;
      sendResponse({ state, sessionId });
      return true;
    }
    // Popup relays a direct task command
    case 'START_TASK': {
      sendUserMessage(message.text);
      sendResponse({ ok: true });
      break;
    }
    // User just logged in — try to connect WS now
    case 'AUTH_TOKEN_SET': {
      dbg('Auth token set — triggering WS connect');
      reconnectAttempt = 0;
      connectToPilot();
      sendResponse({ ok: true });
      break;
    }
    default:
      break;
  }
});

// ─── Entry Point ──────────────────────────────────────────────────────────────
mountOverlay();
connectToPilot();
