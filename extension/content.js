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

// Goal plan — populated when server sends set_plan
let currentPlan = null;

// Heartbeat timer handle and user field-change tracking
let heartbeatTimer = null;
let userModifiedFields = new Set();

// Dedup guard — prevents identical system messages from stacking during reconnect loops
let _lastAddedText = '';

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
        <button id="kb-clear" class="kb-clear" aria-label="Clear chat" title="Clear chat">&#128465;</button>
        <button id="kb-close" class="kb-close" aria-label="Close panel">&times;</button>
      </header>
      <div id="kb-goals" class="kb-goals" hidden aria-label="Task progress">
        <span class="kb-goals-title">Progress</span>
        <ul id="kb-goal-list" class="kb-goal-list" role="list"></ul>
      </div>
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

  const clearBtn = shadow.getElementById('kb-clear');
  clearBtn.addEventListener('click', clearChat);

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

  // If a navigate_to action was mid-flight when the old page unloaded,
  // the extension stored the confirmation in sessionStorage so it could
  // be sent on the new page after reconnect (avoiding ClientDisconnected).
  const pendingNavConfirm = sessionStorage.getItem('kenbot_pending_nav_confirm');
  if (pendingNavConfirm) {
    sessionStorage.removeItem('kenbot_pending_nav_confirm');
    try {
      const confirmPayload = JSON.parse(pendingNavConfirm);
      dbg('Sending deferred navigate_confirmed after page load', confirmPayload.url);
      // Small delay to ensure the backend has processed the reconnect.
      setTimeout(() => safeSend(confirmPayload), 300);
    } catch { /* malformed payload — ignore */ }
    startHeartbeat();
    return;
  }

  // Resume workflow if we navigated cross-origin mid-workflow
  const stored = await chrome.storage.local.get('kenbotSession');
  const pending = stored.kenbotSession?.pendingResume;
  if (pending) {
    // Clear the pendingResume (keep sessionId)
    await chrome.storage.local.set({ kenbotSession: { sessionId } });
    dbg('Resuming workflow after navigation', pending);
    safeSend({ type: 'resume_workflow', ...pending });
    appendSystemMessage('↩️ Continuing workflow…', '↩️ Inaendelea…');
    startHeartbeat();
    return;
  }

  // Resume mid-step if the page was reloaded while a step was executing
  // (e.g. portal-triggered redirect, browser refresh)
  const activeStep = stored.kenbotSession?.activeStep;
  if (activeStep) {
    // Guard against infinite resume loops (e.g. login redirect on every navigation).
    // Allow at most 2 auto-resumes per step_id; beyond that, break the loop and
    // ask the user to take action manually.
    const resumeKey = `rc_${activeStep.step_id || activeStep.service_id || 'unknown'}`;
    const resumeCount = stored.kenbotSession?.resumeCounts?.[resumeKey] || 0;

    if (resumeCount >= 2) {
      // Too many retries for the same step — clear the sentinel and let the
      // backend (via heartbeat / user message) handle recovery.
      dbg('Resume loop detected for', resumeKey, '— clearing activeStep');
      await _clearActiveStep();
      appendSystemMessage(
        '⚠️ The automation seems stuck on this page. Please check what the page needs (e.g. log in) and let me know when done.',
        '⚠️ Mfumo unakwama ukurasa huu. Tafadhali angalia kinachohitajika (k.m. ingia) na niambie unapokuwa tayari.'
      );
      startHeartbeat();
      return;
    }

    // Increment resume count for this step
    const updatedResumeCounts = { ...(stored.kenbotSession?.resumeCounts || {}), [resumeKey]: resumeCount + 1 };
    await chrome.storage.local.set({ kenbotSession: { ...stored.kenbotSession, resumeCounts: updatedResumeCounts } });

    dbg('Resuming mid-step after page reload', activeStep, 'attempt', resumeCount + 1);
    await _clearActiveStep();
    safeSend({ type: 'resume_workflow', ...activeStep, retry_current: true });
    appendSystemMessage('↩️ Resuming…', '↩️ Inaendelea…');
    startHeartbeat();
    return;
  }

  // Only greet on a genuinely fresh session — skip if prior chat history exists.
  const hasHistory = (stored.kenbotSession?.messages?.length || 0) > 0;
  if (!hasHistory) {
    appendSystemMessage('Connected to KenBot. How can I help you?', 'Imeunganishwa na KenBot. Naweza kukusaidia?');
  }
  startHeartbeat();
}

function onWsClose(event) {
  dbg('WebSocket closed code=%d reason=%s', event.code, event.reason || '(none)');
  setStatusIndicator('disconnected');
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;

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

  // Stamp when we disconnected so restoreChatHistory can auto-reset stale sessions.
  if (isContextValid()) {
    chrome.storage.local.get('kenbotSession', (s) => {
      if (chrome.runtime.lastError) return;
      const sess = s.kenbotSession || {};
      chrome.storage.local.set({ kenbotSession: { ...sess, disconnectedAt: Date.now() } });
    });
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
  // Any non-thinking message clears the spinner
  if (msg.type !== 'agent_thinking' && msg.type !== 'heartbeat_ack' && msg.type !== 'state_update') {
    _clearThinking();
  }
  switch (msg.type) {
    case 'agent_message':
      appendAgentMessage(msg.content_en, msg.content_sw);
      break;

    case 'set_plan': {
      currentPlan = msg.goals || [];
      renderGoalPanel(currentPlan, msg.service_name || '');
      // Persist plan so it survives cross-origin navigation
      if (isContextValid() && sessionId) {
        chrome.storage.local.get('kenbotSession', (stored) => {
          if (chrome.runtime.lastError) return;
          const session = stored.kenbotSession || { sessionId };
          chrome.storage.local.set({ kenbotSession: { ...session, plan: currentPlan } });
        });
      }
      break;
    }

    case 'goal_update': {
      if (currentPlan) {
        const goal = currentPlan.find(g => g.id === msg.goal_id);
        if (goal) {
          goal.status = msg.status;
          if (msg.failure_subgoals) goal.failure_subgoals = msg.failure_subgoals;
        }
      }
      updateGoal(msg.goal_id, msg.status, msg.failure_subgoals || []);
      break;
    }

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

    case 'heartbeat_ack':
      break;

    case 'agent_thinking':
      _showThinking(msg.text_en, msg.text_sw);
      break;

    case 'error':
      // Errors are logged to the backend — keep the chat clean.
      dbg('Server error (not shown in chat):', msg.message);
      break;

    case 'captcha_detected':
    case 'await_captcha':
      showCaptchaPrompt();
      break;

    case 'step_complete':
      appendSystemMessage(`✓ ${msg.step_label}`, `✓ ${msg.step_label}`);
      // Clear resume-loop counter for this step on success
      if (isContextValid() && (msg.step_id || msg.step_label)) {
        chrome.storage.local.get('kenbotSession', (snap) => {
          if (chrome.runtime.lastError) return;
          const sess = snap.kenbotSession || {};
          const rc = { ...(sess.resumeCounts || {}) };
          const rKey = `rc_${msg.step_id || msg.step_label}`;
          delete rc[rKey];
          chrome.storage.local.set({ kenbotSession: { ...sess, resumeCounts: rc } });
        });
      }
      break;

    case 'workflow_complete':
      appendSystemMessage(
        `Task complete: ${msg.service_name}`,
        `Kazi imekamilika: ${msg.service_name}`
      );
      hideConfirmationArea();
      break;

    case 'workflow_error':
      dbg('Workflow error (not shown in chat):', msg.message);
      break;

    case 'session_expired':
      appendSystemMessage('Your session has expired. Please log in again.', 'Kipindi chako kimeisha.');
      ws.close(4001);
      break;

    case 'navigate_to': {
      // Autonomous navigation requested by the agent.
      // The current page is about to unload so we CANNOT send over this
      // WebSocket after setting location.href — the connection will close
      // with code 1001 (going away) before the backend can respond.
      // Solution: persist the confirmation in sessionStorage and send it
      // from the NEW page after the WebSocket reconnects in onWsOpen.
      const targetUrl = msg.url || '';
      if (targetUrl) {
        dbg('Autonomous navigate to', targetUrl, '— deferring confirmation to new page');
        sessionStorage.setItem('kenbot_pending_nav_confirm', JSON.stringify({
          type: 'navigate_confirmed',
          url: targetUrl,
          success: true,
        }));
        window.location.href = targetUrl;
      } else {
        safeSend({ type: 'navigate_confirmed', url: '', success: false, error: 'No URL provided' });
      }
      break;
    }

    case 'click_element': {
      // Autonomous click — find element by visible label or aria-label.
      const clickLabel = (msg.label || '').trim().toLowerCase();
      let clicked = false;
      let clickError = '';
      try {
        const candidates = document.querySelectorAll(
          'button, a[href], [role=button], [role=link], input[type=submit], input[type=button]'
        );
        for (const el of candidates) {
          const elText = ((el.textContent || '').trim()
            || el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
          if (elText.includes(clickLabel) || clickLabel.includes(elText.slice(0, 20))) {
            el.click();
            clicked = true;
            break;
          }
        }
        if (!clicked) clickError = `No element found matching label: ${msg.label}`;
      } catch (e) {
        clickError = String(e);
      }
      safeSend({ type: 'free_action_result', label: msg.label, success: clicked, error: clickError });
      break;
    }

    case 'fill_field': {
      // Autonomous field fill — fetch vault value and inject.
      const fieldLabel = (msg.label || '').trim().toLowerCase();
      const vaultKey = msg.vault_key || '';
      (async () => {
        let success = false;
        let fillError = '';
        try {
          // Fetch the value from the vault API
          const resp = await fetch(`/api/vault/retrieve/?key=${encodeURIComponent(vaultKey)}`, {
            headers: { 'X-Session-Id': sessionId || '' },
          });
          if (!resp.ok) throw new Error(`Vault ${resp.status}`);
          const { value } = await resp.json();
          // Find the field by label association or placeholder
          const inputs = document.querySelectorAll('input:not([type=hidden]), textarea');
          for (const el of inputs) {
            const elLabel = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
              || el.name || el.id || '').toLowerCase();
            if (elLabel.includes(fieldLabel) || fieldLabel.includes(elLabel.slice(0, 20))) {
              el.focus();
              el.value = value;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              success = true;
              break;
            }
          }
          if (!success) fillError = `No field found matching label: ${msg.label}`;
        } catch (e) {
          fillError = String(e);
        }
        safeSend({ type: 'free_action_result', label: msg.label, success, error: fillError });
      })();
      break;
    }

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

  // Persist active step so a page reload mid-step can be recovered.
  // Written BEFORE actions execute; cleared on confirmed or failed.
  if (isContextValid()) {
    const snap = await chrome.storage.local.get('kenbotSession');
    const session = snap.kenbotSession || { sessionId };
    await chrome.storage.local.set({
      kenbotSession: {
        ...session,
        activeStep: {
          service_id: currentServiceId,
          step_id: stepId,
          step_index: currentStepIndex,
          total_steps: currentTotalSteps,
        },
      },
    });
  }

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
    await _clearActiveStep();
    safeSend({ type: 'step_confirmed', step_id: stepId });
  } catch (err) {
    dbg('executeStep error', stepId, err);
    await _clearActiveStep();
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

/**
 * Clear the chat history, goal plan, and any thinking indicators.
 * Wipes chrome.storage.local messages + plan while keeping the sessionId.
 */
async function clearChat() {
  const log = shadow && shadow.getElementById('kb-messages');
  if (log) log.innerHTML = '';
  currentPlan = null;
  const goalsEl = shadow && shadow.getElementById('kb-goals');
  if (goalsEl) goalsEl.hidden = true;
  if (isContextValid()) {
    const stored = await chrome.storage.local.get('kenbotSession');
    const sess = stored.kenbotSession || {};
    delete sess.messages;
    delete sess.plan;
    delete sess.disconnectedAt;
    delete sess.activeStep;
    sess.resumeCounts = {};
    await chrome.storage.local.set({ kenbotSession: sess });
    safeSend({ type: 'reset_session' });
  }
  appendSystemMessage('Chat cleared.', 'Mazungumzo yamefutwa.');
}

/** Ephemeral ID of the currently-displayed thinking bubble (if any). */
let _thinkingBubbleId = null;

/**
 * Show (or replace) a thinking/progress indicator in the chat.
 * These bubbles are NOT persisted to storage — they disappear on reload.
 * @param {string} textEn
 * @param {string} textSw
 */
function _showThinking(textEn, textSw) {
  if (!shadow) return;
  const log = shadow.getElementById('kb-messages');
  if (!log) return;

  // Remove previous thinking bubble if still visible
  if (_thinkingBubbleId) {
    const prev = log.querySelector(`[data-thinking-id="${_thinkingBubbleId}"]`);
    if (prev) prev.remove();
  }

  _thinkingBubbleId = `t-${Date.now()}`;
  const bubble = document.createElement('div');
  bubble.className = 'kb-message kb-message--thinking';
  bubble.dataset.thinkingId = _thinkingBubbleId;

  const enSpan = document.createElement('span');
  enSpan.className = 'kb-msg-en';
  enSpan.textContent = textEn;
  bubble.appendChild(enSpan);

  if (textSw && textSw !== textEn) {
    bubble.appendChild(document.createElement('br'));
    const swSpan = document.createElement('span');
    swSpan.className = 'kb-msg-sw';
    swSpan.textContent = textSw;
    bubble.appendChild(swSpan);
  }

  log.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
}

/**
 * Remove the current thinking bubble (called when a real message arrives).
 */
function _clearThinking() {
  if (!shadow || !_thinkingBubbleId) return;
  const log = shadow.getElementById('kb-messages');
  if (!log) return;
  const prev = log.querySelector(`[data-thinking-id="${_thinkingBubbleId}"]`);
  if (prev) prev.remove();
  _thinkingBubbleId = null;
}



function appendAgentMessage(textEn, textSw) {
  addMessage('agent', textEn, textSw);
}

function appendUserMessage(text) {
  _lastAddedText = '';  // reset dedup guard so the next system/agent message is always shown
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
  // Skip if this exact text was the last thing appended (prevents log spam
  // during reconnect loops where the same step message repeats).
  if (role !== 'user' && textEn === _lastAddedText) return;
  _lastAddedText = textEn;

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

  // Auto-reset if the session has been idle for more than 3 minutes.
  const IDLE_RESET_MS = 3 * 60 * 1000;
  const disconnectedAt = stored.kenbotSession?.disconnectedAt;
  if (disconnectedAt && (Date.now() - disconnectedAt) > IDLE_RESET_MS) {
    dbg('Chat history stale (>3 min idle) — resetting session');
    await chrome.storage.local.set({
      kenbotSession: { sessionId: stored.kenbotSession?.sessionId },
    });
    return;
  }

  const messages = stored.kenbotSession?.messages;
  if (messages && messages.length) {
    for (const msg of messages) {
      _renderBubble(msg.role, msg.textEn, msg.textSw || msg.textEn);
    }
  }
  // Restore goal plan if one was active
  const storedPlan = stored.kenbotSession?.plan;
  if (storedPlan && storedPlan.length) {
    currentPlan = storedPlan;
    renderGoalPanel(storedPlan, '');
  }
}

// ─── Goal Panel ──────────────────────────────────────────────────────────────

/**
 * Render (or re-render) the goal panel above the message log.
 * @param {Array<{id, label, status, step_ids, failure_subgoals, is_prerequisite}>} goals
 * @param {string} serviceName
 */
function renderGoalPanel(goals, serviceName) {
  if (!shadow) return;
  const goalsEl = shadow.getElementById('kb-goals');
  const list = shadow.getElementById('kb-goal-list');
  if (!goalsEl || !list) return;

  list.innerHTML = '';
  for (const goal of goals) {
    list.appendChild(_buildGoalItem(goal));
  }
  goalsEl.hidden = goals.length === 0;
}

/**
 * Build a single <li> element for a goal node.
 * @param {object} goal
 * @returns {HTMLLIElement}
 */
function _buildGoalItem(goal) {
  const li = document.createElement('li');
  const status = goal.status || 'pending';
  li.className = `kb-goal-item kb-goal-item--${status}${goal.is_prerequisite ? ' kb-goal-item--prereq' : ''}`;
  li.dataset.goalId = goal.id;
  li.setAttribute('role', 'listitem');

  const icon = document.createElement('span');
  icon.className = 'kb-goal-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = _goalIcon(status);

  const label = document.createElement('span');
  label.className = 'kb-goal-label';
  label.textContent = goal.label;

  li.appendChild(icon);
  li.appendChild(label);

  // If already failed on restore, show subgoal buttons immediately
  if (status === 'failed' && goal.failure_subgoals && goal.failure_subgoals.length) {
    li.appendChild(_buildSubgoalList(goal.failure_subgoals, li));
  }

  return li;
}

/**
 * Update an existing goal item's status and optionally show subgoal buttons.
 * @param {string} goalId
 * @param {'pending'|'running'|'done'|'failed'} status
 * @param {Array} subgoals
 */
function updateGoal(goalId, status, subgoals) {
  if (!shadow) return;
  const list = shadow.getElementById('kb-goal-list');
  if (!list) return;

  const item = list.querySelector(`[data-goal-id="${CSS.escape(goalId)}"]`);
  if (!item) return;

  // Replace status class
  item.className = item.className
    .replace(/\bkb-goal-item--(?:pending|running|done|failed)\b/g, '')
    .trim() + ` kb-goal-item--${status}`;

  // Update icon
  const icon = item.querySelector('.kb-goal-icon');
  if (icon) icon.textContent = _goalIcon(status);

  // Remove any previous subgoal list
  const existingSg = item.querySelector('.kb-subgoal-list');
  if (existingSg) existingSg.remove();

  // Show subgoal buttons when goal fails
  if (status === 'failed' && subgoals && subgoals.length) {
    item.appendChild(_buildSubgoalList(subgoals, item));
  }
}

/**
 * Build the subgoal <ul> with clickable option buttons.
 * @param {Array<{label, action, service_id}>} subgoals
 * @param {HTMLElement} parentItem  — the parent goal <li> (to remove list on click)
 * @returns {HTMLUListElement}
 */
function _buildSubgoalList(subgoals, parentItem) {
  const ul = document.createElement('ul');
  ul.className = 'kb-subgoal-list';
  ul.setAttribute('role', 'list');

  for (const sg of subgoals) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.className = 'kb-subgoal-btn';
    btn.type = 'button';
    btn.textContent = sg.label;
    btn.addEventListener('click', () => {
      safeSend({
        type: 'subgoal_selected',
        action: sg.action,
        service_id: sg.service_id || null,
      });
      ul.remove();
    });
    li.appendChild(btn);
    ul.appendChild(li);
  }

  return ul;
}

/** Return a text character icon for a goal status. */
function _goalIcon(status) {
  switch (status) {
    case 'running': return '●';
    case 'done':    return '✓';
    case 'failed':  return '✗';
    default:        return '○';   // pending
  }
}

// ─────────────────────────────────────────────────────────────────────────────

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

// ─── Active-step helpers (Phase 1 reconnection) ──────────────────────────────

/**
 * Remove the activeStep sentinel from storage on clean step completion or failure.
 * Leaves sessionId, messages, and plan intact.
 */
async function _clearActiveStep() {
  if (!isContextValid()) return;
  try {
    const snap = await chrome.storage.local.get('kenbotSession');
    const session = snap.kenbotSession || { sessionId };
    delete session.activeStep;
    await chrome.storage.local.set({ kenbotSession: session });
  } catch { /* silent */ }
}

// ─── Heartbeat (Phase 2) ─────────────────────────────────────────────────────

/**
 * Start the 15-second heartbeat interval.
 * Safe to call multiple times — clears any existing timer first.
 */
function startHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(sendHeartbeat, 15000);
  dbg('Heartbeat started');
}

/**
 * Collect a lightweight page snapshot and send it to the server.
 * SECURITY: only field names collected, never field values.
 */
function sendHeartbeat() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  // Collect visible form field names (never values)
  const visibleFields = [];
  try {
    document.querySelectorAll('input:not([type=hidden]), select, textarea').forEach((el) => {
      const label = el.name || el.id || el.getAttribute('aria-label') || '';
      if (label) visibleFields.push(label);
    });
  } catch { /* cross-origin frame — ignore */ }

  // Quick heuristic scans of visible text for error/success states
  const bodyText = (() => {
    try { return document.body.innerText || ''; } catch { return ''; }
  })();
  const hasError = /error|failed|invalid|incorrect|try again|please enter/i.test(bodyText);
  const hasSuccess = /success|submitted|confirmed|thank you|complete|approved/i.test(bodyText);

  // Collect interactive element labels (buttons, links) — labels only, never values
  const interactiveElements = [];
  try {
    document.querySelectorAll('button, a[href], [role=button], [role=link]').forEach((el) => {
      if (interactiveElements.length >= 40) return;
      const label = (el.textContent || '').trim().slice(0, 60)
        || el.getAttribute('aria-label') || el.getAttribute('title') || '';
      if (label) interactiveElements.push(label);
    });
  } catch { /* cross-origin frame — ignore */ }

  const payload = {
    type: 'heartbeat',
    url: location.href,
    title: document.title,
    page_text_preview: bodyText.slice(0, 1000),
    visible_fields: visibleFields.slice(0, 30), // cap to avoid bloat
    interactive_elements: interactiveElements,
    has_error: hasError,
    has_success: hasSuccess,
    user_modified_fields: [...userModifiedFields],
  };

  safeSend(payload);
  userModifiedFields = new Set(); // reset after each heartbeat
  dbg('Heartbeat sent', { url: payload.url, has_error: hasError, has_success: hasSuccess });
}

/**
 * Track field names the user is actively editing on the portal.
 * Filters out KenBot's own shadow DOM to avoid feedback loops.
 * SECURITY: only e.target.name / id collected, never .value.
 */
function trackUserInput(e) {
  // Ignore events originating inside the KenBot overlay
  try {
    if (e.composedPath().some((n) => n && n.id === 'kenbot-host')) return;
  } catch { return; }
  const fieldName = e.target.name || e.target.id || e.target.getAttribute('aria-label') || 'unnamed';
  if (fieldName !== 'unnamed') userModifiedFields.add(fieldName);
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

// Listen for user-initiated field edits on the portal page (heartbeat tracking).
// Using capture=true so we see events even inside iframes on the same origin.
document.addEventListener('input', trackUserInput, { capture: true, passive: true });
document.addEventListener('change', trackUserInput, { capture: true, passive: true });
