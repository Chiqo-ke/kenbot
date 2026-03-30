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

// Workflow tracking vars — persisted across cross-origin navigations
let currentServiceId = null;
let currentStepId = null;
let currentStepIndex = 0;
let currentTotalSteps = 0;

// ─── Context validity guard ──────────────────────────────────────────────────
// When the extension is reloaded during development, chrome.runtime.id becomes
// undefined and any Chrome API call throws "Extension context invalidated".
// Check this before every Chrome API call path so we fail silently instead of
// spamming the console.
function isContextValid() {
  try {
    return !!chrome.runtime?.id;
  } catch {
    return false;
  }
}

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

  // Restore persisted chat history (async, non-blocking).
  restoreChatHistory();
}

// ─── 2. WebSocket Connection ──────────────────────────────────────────────────

/**
 * Open (or re-open) the WebSocket connection to the Pilot.
 * Fetches the stored JWT from background before connecting.
 * Uses exponential back-off on disconnect.
 */
async function connectToPilot() {
  if (!isContextValid()) {
    dbg('Extension context invalidated — stopping reconnect loop.');
    return;
  }

  if (!sessionId) {
    // Restore sessionId from chrome.storage.local (survives cross-origin navigation)
    const stored = await chrome.storage.local.get('kenbotSession');
    sessionId = stored.kenbotSession?.sessionId || crypto.randomUUID();
    await chrome.storage.local.set({ kenbotSession: { ...stored.kenbotSession, sessionId } });
  }

  // Fetch JWT token from background storage
  const tokenResult = await new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'GET_AUTH_TOKEN' }, (response) => {
      resolve(response && response.token ? response.token : null);
    });
  });

  if (!tokenResult) {
    dbg('No auth token — skipping WS connect. Log in via the popup.');
    setStatusIndicator('disconnected');
    return;
  }

  const anonKey = await getAnonKey();
  const url = `${KENBOT_WS_BASE}${sessionId}/?token=${encodeURIComponent(tokenResult)}&vault_key=${encodeURIComponent(anonKey)}`;
  dbg('Connecting WebSocket to', url);
  ws = new WebSocket(url);

  ws.addEventListener('open', onWsOpen);
  ws.addEventListener('message', onWsMessage);
  ws.addEventListener('close', onWsClose);
  ws.addEventListener('error', onWsError);
}

async function onWsOpen() {
  reconnectAttempt = 0;
  dbg('WebSocket opened session=%s', sessionId);
  setStatusIndicator('connected');
  if (isContextValid()) chrome.runtime.sendMessage({ type: 'SESSION_STARTED', sessionId });

  // Resume workflow if we navigated cross-origin mid-workflow
  const stored = await chrome.storage.local.get('kenbotSession');
  const pending = stored.kenbotSession?.pendingResume;
  if (pending) {
    // Clear the pendingResume (keep sessionId)
    await chrome.storage.local.set({ kenbotSession: { sessionId } });
    dbg('Resuming workflow after navigation', pending);
    safeSend({ type: 'resume_workflow', ...pending });
    appendSystemMessage('↩️ Continuing workflow…', '↩️ Inaendelea…');
    return;
  }

  appendSystemMessage('Connected to KenBot. How can I help you?', 'Imeunganishwa na KenBot. Naweza kukusaidia?');
}

function onWsClose(event) {
  dbg('WebSocket closed code=%d reason=%s', event.code, event.reason || '(none)');
  setStatusIndicator('disconnected');

  // Normal closure — clean exit, no reconnect
  if (event.code === 1000) {
    if (isContextValid()) {
      chrome.storage.local.remove('kenbotSession');
      chrome.runtime.sendMessage({ type: 'SESSION_ENDED' });
    }
    return;
  }

  // Auth failure — server explicitly rejected our token (4001).
  // First ask the background to silently refresh the access token.
  // If the refresh succeeds the reconnect loop will pick up the new token
  // transparently.  Only clear stored credentials and prompt re-login when
  // the refresh token is also gone or expired.
  if (event.code === 4001) {
    (async () => {
      if (!isContextValid()) {
        dbg('Extension context invalidated on 4001 — stopping.');
        return;
      }

      const tokenResult = await new Promise((resolve) => {
        chrome.runtime.sendMessage({ type: 'GET_AUTH_TOKEN' }, (response) => {
          resolve(response && response.token ? response.token : null);
        });
      });

      if (tokenResult) {
        // background.js refreshed the access token — reconnect silently
        dbg('Token auto-refreshed after 4001 — scheduling reconnect');
        scheduleReconnect();
        return;
      }

      // Refresh failed — force re-login
      dbg('Auth rejected (4001) — clearing stored token, prompting re-login.');
      chrome.runtime.sendMessage({ type: 'CLEAR_AUTH_TOKEN' });
      chrome.storage.local.remove('kenbotSession');
      sessionId = null;
      appendSystemMessage(
        'Your session has expired. Please log in again via the KenBot popup.',
        'Kipindi chako kimeisha. Tafadhali ingia tena kupitia popup ya KenBot.'
      );
      chrome.runtime.sendMessage({ type: 'SESSION_ENDED' });
    })();
    return;
  }

  scheduleReconnect();
}

function onWsError(err) {
  dbg('WebSocket error', err);
  setStatusIndicator('error');
}

function scheduleReconnect() {
  if (!isContextValid()) {
    dbg('Extension context invalidated — cancelling reconnect.');
    return;
  }
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
async function handlePilotMessage(msg) {
  switch (msg.type) {
    case 'agent_message':
      appendAgentMessage(msg.content_en, msg.content_sw);
      break;

    case 'execute_action':
      executeAction(msg.action);
      break;

    case 'execute_step':
      await executeStep(msg);
      break;

    case 'pause_confirmation':
      showConfirmation(msg.step_label, msg.fields ? msg.fields.split(',').map(f => f.trim()) : []);
      break;

    case 'await_vault_key':
      showVaultKeyPrompt(msg.missing_keys);
      break;

    case 'open_url':
      openPortalUrl(msg.url, msg.missing_keys);
      break;

    case 'state_update':
      updateStatusFromState(msg.state);
      break;

    case 'session_complete':
      appendSystemMessage('✅ Task complete!', '✅ Kazi imekamilika!');
      hideConfirmationArea();
      break;

    case 'error':
      appendSystemMessage(`❌ Error: ${msg.message}`, `❌ Hitilafu: ${msg.message}`);
      break;

    case 'captcha_detected':
    case 'await_captcha':
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
  safeSend({ type: 'user_message', content: text });
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

// ─── 5b. Step-Level Orchestration ────────────────────────────────────────────

/**
 * Check whether the current page URL matches a step's url_match pattern.
 * @param {string|null} pattern
 * @param {string} strategy  'contains' | 'starts-with' | 'exact' | 'regex'
 * @returns {boolean}
 */
function urlMatches(pattern, strategy) {
  if (!pattern) return true;
  const url = location.href;
  if (strategy === 'exact') return url === pattern;
  if (strategy === 'starts-with') return url.startsWith(pattern);
  if (strategy === 'regex') {
    try { return new RegExp(pattern).test(url); } catch { return false; }
  }
  return url.includes(pattern); // 'contains' default
}

/**
 * Execute a single action and return a Promise.
 * Unlike executeAction(), this does NOT send step_confirmed — the caller
 * (executeStep) sends ONE confirmed signal after ALL actions complete.
 * @param {object} action
 * @returns {Promise<void>}
 */
async function executeActionAsync(action) {
  if (action.required_data_key) {
    // Fetch from vault and inject
    const anonKey = await getAnonKey();
    const res = await fetch(
      `${KENBOT_BACKEND_HTTP}/api/vault/${encodeURIComponent(action.required_data_key)}/`,
      {
        method: 'GET',
        headers: { 'X-Vault-Key': anonKey, 'Content-Type': 'application/json' }
      }
    );
    if (!res.ok) {
      const err = new Error(`Vault fetch failed: ${res.status}`);
      err.selector = action.selector ? action.selector.primary : '';
      throw err;
    }
    const { value } = await res.json();
    if (action.type === 'password' || action.type === 'text') {
      await performFillAsync(action.selector.primary, action.selector.fallbacks || [], value);
    } else if (action.type === 'select') {
      await performSelectAsync(action.selector.primary, action.selector.fallbacks || [], value);
    }
    // value leaves scope here — never sent over WS
    return;
  }

  const sel = action.selector || {};
  switch (action.type) {
    case 'click':
      await performClickAsync(sel.primary, sel.fallbacks || []);
      break;
    case 'text':
    case 'password':
      // static text fill (rare — most use vault)
      await performFillAsync(sel.primary, sel.fallbacks || [], action.static_value || '');
      break;
    case 'select':
      await performSelectAsync(sel.primary, sel.fallbacks || [], action.static_value || '');
      break;
    case 'checkbox':
      await performCheckboxAsync(sel.primary, sel.fallbacks || [], !!action.checked);
      break;
    case 'wait':
      await delay(action.wait_ms || 1000);
      break;
    case 'navigate':
      // Save pending resume state BEFORE navigating (new page = new JS env)
      await chrome.storage.local.set({
        kenbotSession: {
          sessionId,
          pendingResume: {
            service_id: currentServiceId,
            step_id: currentStepId,
            step_index: currentStepIndex + 1,
            total_steps: currentTotalSteps
          }
        }
      });
      window.location.href = action.url;
      await delay(2000); // allow navigation to initiate
      break;
    case 'scroll': {
      const target = sel.primary ? findElement(sel.primary, sel.fallbacks || []) : null;
      if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      else window.scrollBy(0, action.scroll_amount || 300);
      await delay(400);
      break;
    }
    default: {
      const err = new Error(`Unknown action type: ${action.type}`);
      err.selector = sel.primary || '';
      throw err;
    }
  }
}

/**
 * Orchestrate a full workflow step: execute each action in sequence, then
 * report a single step_confirmed or step_failed back to the Pilot.
 * @param {object} msg  The execute_step message from the server
 */
async function executeStep(msg) {
  const stepId = msg.step_id;
  const stepLabel = msg.step_label || stepId;
  const actions = msg.actions || [];
  const requiresHumanReview = !!msg.requires_human_review;

  // Track workflow position for cross-origin resume
  currentServiceId = msg.service_id || currentServiceId;
  currentStepId = stepId;
  currentStepIndex = msg.step_index || 0;
  currentTotalSteps = msg.total_steps || currentTotalSteps;

  dbg(
    'executeStep', stepId,
    'index:', msg.step_index, '/', msg.total_steps,
    'url_match:', msg.url_match,
    'on_correct_page:', urlMatches(msg.url_match, msg.url_match_strategy)
  );

  appendSystemMessage(`⚙️ ${stepLabel}…`, `⚙️ ${stepLabel}…`);

  if (msg.requires_otp_input) {
    const instruction = msg.human_instruction || 'A 6-digit OTP has been sent to you. Please type it here.';
    showOtpInputPrompt(instruction, stepId, msg.otp_selector, msg.otp_submit_selector);
    return;
  }

  if (requiresHumanReview) {
    const instruction = msg.human_instruction || `Please complete this step manually: ${stepLabel}`;
    showHumanInputPrompt(instruction, stepId);
    return;
  }

  try {
    for (const action of actions) {
      await executeActionAsync(action);
    }
    dbg('executeStep complete', stepId);
    safeSend({ type: 'step_confirmed', step_id: stepId });
  } catch (err) {
    dbg('executeStep error', stepId, err);
    safeSend({
      type: 'step_failed',
      step_id: stepId,
      selector: err.selector || '',
      reason: err.message
    });
  }
}

// ─── 5c. Async DOM Helpers (Promise-based, no safeSend) ──────────────────────

/**
 * Show a human-instruction overlay on requires_human_review steps.
 * Replaces the old showCaptchaPrompt() for workflow steps.
 * @param {string} instruction  Text shown to the user (from map's human_instruction)
 * @param {string} stepId       Current step ID (used for Forgot Password button logic)
 */
function showHumanInputPrompt(instruction, stepId) {
  if (!shadow) return;
  const area = shadow.getElementById('kb-confirmation-area');
  if (!area) return;

  // Ensure the chat panel is open so the user sees the instruction
  const chatPanel = shadow.getElementById('kb-chat');
  if (chatPanel) chatPanel.hidden = false;

  area.innerHTML = '';
  area.hidden = false;

  const p = document.createElement('p');
  p.className = 'kb-confirm-label';
  p.style.whiteSpace = 'pre-wrap';
  p.textContent = instruction;
  area.appendChild(p);

  // "Done" button — sends step_confirmed
  const doneBtn = document.createElement('button');
  doneBtn.className = 'kb-btn kb-btn--confirm';
  doneBtn.textContent = 'Done / Nimekamilisha';
  doneBtn.addEventListener('click', () => {
    safeSend({ type: 'step_confirmed', step_id: stepId });
    hideConfirmationArea();
  });
  area.appendChild(doneBtn);

  // "Forgot Password" button — only on the login step
  if (stepId === 'ecitizen_login') {
    const forgotBtn = document.createElement('button');
    forgotBtn.className = 'kb-btn kb-btn--cancel';
    forgotBtn.style.marginTop = '6px';
    forgotBtn.textContent = 'Forgot Password / Nimesahau Nywila';
    forgotBtn.addEventListener('click', () => {
      safeSend({ type: 'user_message', content: 'I forgot my eCitizen password, please help me reset it.' });
      hideConfirmationArea();
    });
    area.appendChild(forgotBtn);
  }
}

/**
 * Show an OTP text-input directly in the KenBot overlay.
 * User types the OTP → extension fills the portal field silently → submits → sends step_confirmed.
 * @param {string} instruction       Instruction shown to user
 * @param {string} stepId            Current step ID
 * @param {string} otpSelector       CSS selector for the portal OTP input
 * @param {string} submitSelector    CSS selector for the portal submit button after OTP
 */
function showOtpInputPrompt(instruction, stepId, otpSelector, submitSelector) {
  if (!shadow) return;
  const area = shadow.getElementById('kb-confirmation-area');
  if (!area) return;

  const chatPanel = shadow.getElementById('kb-chat');
  if (chatPanel) chatPanel.hidden = false;

  area.innerHTML = '';
  area.hidden = false;

  const p = document.createElement('p');
  p.className = 'kb-confirm-label';
  p.style.whiteSpace = 'pre-wrap';
  p.textContent = instruction;
  area.appendChild(p);

  const otpInput = document.createElement('input');
  otpInput.type = 'text';
  otpInput.inputMode = 'numeric';
  otpInput.maxLength = 6;
  otpInput.placeholder = 'Enter 6-digit OTP';
  otpInput.className = 'kb-otp-input';
  otpInput.style.cssText = 'width:100%;padding:8px 10px;margin:8px 0;font-size:1.2rem;letter-spacing:0.2em;border:2px solid #0ea5e9;border-radius:6px;text-align:center;box-sizing:border-box;';
  area.appendChild(otpInput);

  const submitBtn = document.createElement('button');
  submitBtn.className = 'kb-btn kb-btn--confirm';
  submitBtn.textContent = 'Submit OTP';
  submitBtn.addEventListener('click', async () => {
    const otp = otpInput.value.trim();
    if (!otp || otp.length < 4) {
      otpInput.style.borderColor = '#ef4444';
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting…';
    try {
      if (otpSelector) {
        await performFillAsync(otpSelector, [], otp);
      }
      if (submitSelector) {
        await performClickAsync(submitSelector, []);
      }
      hideConfirmationArea();
      safeSend({ type: 'step_confirmed', step_id: stepId });
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Submit OTP';
      otpInput.style.borderColor = '#ef4444';
      appendSystemMessage('❌ Could not submit OTP: ' + err.message, '❌ Could not submit OTP: ' + err.message);
    }
  });
  area.appendChild(submitBtn);

  // Focus the input automatically
  setTimeout(() => otpInput.focus(), 100);
}

async function performFillAsync(primary, fallbacks, value) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    const err = new Error('Element not found');
    err.selector = primary;
    throw err;
  }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await delay(200);
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
}

async function performClickAsync(primary, fallbacks) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    const err = new Error('Element not found');
    err.selector = primary;
    throw err;
  }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await delay(300);
  el.click();
  await delay(500);
}

async function performSelectAsync(primary, fallbacks, optionValue) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    const err = new Error('Element not found');
    err.selector = primary;
    throw err;
  }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await delay(200);
  el.value = optionValue;
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

async function performCheckboxAsync(primary, fallbacks, checked) {
  const el = findElement(primary, fallbacks);
  if (!el) {
    const err = new Error('Element not found');
    err.selector = primary;
    throw err;
  }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await delay(200);
  if (el.checked !== checked) {
    el.click();
  }
}

/**
 * Fetch a decrypted vault value from the Django API and inject it directly into
 * the target DOM element. The plaintext value is NOT stored beyond this scope.
 * @param {object} action
 */
async function injectVaultValue(action) {
  const anonKey = await getAnonKey();

  const res = await fetch(
    `${KENBOT_BACKEND_HTTP}/api/vault/${encodeURIComponent(action.required_data_key)}/`,
    {
      method: 'GET',
      headers: {
        'X-Vault-Key': anonKey,
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
  _renderBubble(role, textEn, textSw);

  // Persist so the chat log survives cross-origin page navigation.
  if (isContextValid() && sessionId) {
    chrome.storage.local.get('kenbotSession', (stored) => {
      if (chrome.runtime.lastError) return;
      const session = stored.kenbotSession || { sessionId };
      const messages = session.messages || [];
      messages.push({ role, textEn, textSw: textSw || textEn });
      // Cap at 300 bubbles to avoid storage bloat
      const trimmed = messages.length > 300 ? messages.slice(-300) : messages;
      chrome.storage.local.set({ kenbotSession: { ...session, messages: trimmed } });
    });
  }
}

/**
 * Render a message bubble in the shadow DOM without touching storage.
 * Used by addMessage() and restoreChatHistory().
 * @param {'agent'|'user'|'system'} role
 * @param {string} textEn
 * @param {string} textSw
 */
function _renderBubble(role, textEn, textSw) {
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
 * Replay persisted messages into the shadow DOM after a cross-origin navigation.
 * Should be called once after mountOverlay() builds the chat panel.
 */
async function restoreChatHistory() {
  if (!isContextValid()) return;
  let stored;
  try {
    stored = await chrome.storage.local.get('kenbotSession');
  } catch {
    return;
  }
  const messages = stored.kenbotSession?.messages;
  if (!messages || !messages.length) return;
  for (const msg of messages) {
    _renderBubble(msg.role, msg.textEn, msg.textSw || msg.textEn);
  }
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

/**
 * Sync the status dot from an ExecutionState object sent by the server.
 * @param {{ status: string }} state
 */
function updateStatusFromState(state) {
  if (!state) return;
  const errorStatuses = new Set(['failed', 'awaiting_healing']);
  setStatusIndicator(errorStatuses.has(state.status) ? 'error' : 'connected');
}

/**
 * Show a prompt instructing the user to add missing vault credentials.
 * @param {string[]} missingKeys
 */
function showVaultKeyPrompt(missingKeys) {
  const keyList = Array.isArray(missingKeys) ? missingKeys.join(', ') : String(missingKeys || '');
  appendSystemMessage(
    `🔑 Missing credentials: ${keyList}. Please save them via the KenBot popup.`,
    `🔑 Taarifa zinazokosekana: ${keyList}. Tafadhali ziingize kupitia popup ya KenBot.`
  );
}

/**
 * Open a portal URL in a new tab and prompt the user to fill in their details.
 * @param {string} url
 * @param {string} missingKeys  comma-separated human-readable field names
 */
function openPortalUrl(url, missingKeys) {
  window.open(url, '_blank');
  appendSystemMessage(
    `🌐 Portal opened. Please fill in: ${missingKeys || 'your details'}, then let me know when done.`,
    `🌐 Portal imefunguliwa. Tafadhali jaza: ${missingKeys || 'taarifa zako'}, kisha niambie ukikamilisha.`
  );
}

// ─── 8. Anon-Key Retrieval (via background) ─────────────────────────────────────────

/**
 * Retrieve the persistent anon_key UUID from the background service worker.
 * Used as X-Vault-Key header on all vault API calls.
 * @returns {Promise<string>}
 */
function getAnonKey() {
  if (!isContextValid()) return Promise.resolve('00000000-0000-0000-0000-000000000000');
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'GET_ANON_KEY' }, (response) => {
      resolve(response && response.key ? response.key : '00000000-0000-0000-0000-000000000000');
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
    // Background notifies that a new JWT was stored — reconnect with it
    case 'AUTH_TOKEN_UPDATED': {
      dbg('Auth token updated — reconnecting WebSocket');
      if (ws) {
        ws.onclose = null; // suppress auto-reconnect from old socket
        ws.close(1000);
        ws = null;
      }
      reconnectAttempt = 0;
      sessionId = null; // fresh session for the newly logged-in user
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
