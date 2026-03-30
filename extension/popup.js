// popup.js — KenBot extension popup
// Plain JS, no imports, no bundler.
//
// Responsibilities:
//   - Auth: login / logout via popup, stores JWT in chrome.storage.local
//   - Show connection status (polls content script via background)
//   - Relay a quick-start task command to the active tab's content script
//
// DEBUG: Open the extension popup, right-click → Inspect popup → Console
//        Filter by "[KenBot:popup]" to see all events.

'use strict';

function dbg(...args) { console.debug('[KenBot:popup]', ...args); }

const KENBOT_BACKEND_HTTP = 'http://127.0.0.1:8000';

// ─── DOM refs (resolved after DOMContentLoaded) ───────────────────────────────
let statusDot, statusText, sessionInfoEl;
let taskForm, taskInput;
let loginForm, usernameInput, passwordInput;
let loggedOutView, loggedInView;
let toastEl;

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  statusDot     = document.getElementById('popup-status-dot');
  statusText    = document.getElementById('status-text');
  sessionInfoEl = document.getElementById('session-info');
  taskForm      = document.getElementById('task-form');
  taskInput     = document.getElementById('task-input');
  loginForm     = document.getElementById('login-form');
  usernameInput = document.getElementById('username-input');
  passwordInput = document.getElementById('password-input');
  loggedOutView = document.getElementById('logged-out-view');
  loggedInView  = document.getElementById('logged-in-view');
  toastEl       = document.getElementById('toast');

  // Attach events
  taskForm.addEventListener('submit', onTaskSubmit);
  loginForm.addEventListener('submit', onLoginSubmit);
  document.getElementById('logout-btn').addEventListener('click', onLogout);

  // Initial state load
  checkAuthState();
});


function refreshStatus() {
  // Ask background for session state
  chrome.runtime.sendMessage({ type: 'GET_SESSION_STATUS' }, (response) => {
    if (chrome.runtime.lastError || !response) {
      setStatus('disconnected', 'No active portal tab');
      return;
    }
    if (response.active) {
      setStatus('connected', 'Connected');
      showSessionId(response.sessionId);
    } else {
      // Try asking the content script directly
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs[0];
        if (!tab) { setStatus('disconnected', 'No active tab'); return; }

        chrome.tabs.sendMessage(tab.id, { type: 'GET_WS_STATUS' }, (res) => {
          if (chrome.runtime.lastError || !res) {
            setStatus('disconnected', 'KenBot not active on this page');
            return;
          }
          const stateLabels = { 0: 'Connecting…', 1: 'Connected', 2: 'Closing…', 3: 'Disconnected' };
          const stateClass  = res.state === 1 ? 'connected' : 'disconnected';
          setStatus(stateClass, stateLabels[res.state] || 'Unknown');
          if (res.state === 1) showSessionId(res.sessionId);
        });
      });
    }
  });
}

/**
 * @param {'connected'|'disconnected'|'error'} state
 * @param {string} label
 */
function setStatus(state, label) {
  statusDot.className = `status-dot ${state}`;
  statusDot.setAttribute('title', label);
  statusText.textContent = label;
}

function showSessionId(sessionId) {
  if (!sessionId) return;
  sessionInfoEl.textContent = `Session: ${sessionId}`;
  sessionInfoEl.hidden = false;
}

// ─── Auth State ───────────────────────────────────────────────────────────────

function checkAuthState() {
  chrome.runtime.sendMessage({ type: 'GET_AUTH_TOKEN' }, (response) => {
    const hasToken = response && response.token;
    loggedOutView.hidden = hasToken;
    loggedInView.hidden  = !hasToken;
    if (hasToken) refreshStatus();
  });
}

async function onLoginSubmit(e) {
  e.preventDefault();
  const username = usernameInput.value.trim();
  const password = passwordInput.value;

  if (!username || !password) {
    showToast('Please enter username and password.');
    return;
  }

  dbg('Attempting login for', username, '→', `${KENBOT_BACKEND_HTTP}/api/auth/token/`);

  try {
    const res = await fetch(`${KENBOT_BACKEND_HTTP}/api/auth/token/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });

    dbg('Login response status:', res.status);

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      dbg('Login error body:', err);
      showToast(err.detail || `Login failed (HTTP ${res.status}). Check credentials.`);
      return;
    }

    const data = await res.json();
    dbg('Login success — storing token');
    const { access, refresh } = data;
    chrome.runtime.sendMessage({ type: 'SET_AUTH_TOKEN', token: access, refreshToken: refresh }, () => {
      passwordInput.value = ''; // Clear password from DOM
      loggedOutView.hidden = true;
      loggedInView.hidden  = false;
      showToast('Logged in successfully.');
      // Refresh status after a short delay to give content script time to connect
      setTimeout(refreshStatus, 1500);
    });
  } catch (err) {
    dbg('Login fetch error:', err);
    showToast(`Network error: ${err.message}. Is the backend running on port 8000?`);
  }
}

function onLogout() {
  chrome.runtime.sendMessage({ type: 'CLEAR_AUTH_TOKEN' }, () => {
    loggedOutView.hidden = false;
    loggedInView.hidden  = true;
    showToast('Logged out.');
  });
}

// ─── Quick-task Relay ─────────────────────────────────────────────────────────

function onTaskSubmit(e) {
  e.preventDefault();
  const text = taskInput.value.trim();
  if (!text) return;

  chrome.runtime.sendMessage(
    { type: 'RELAY_TO_CONTENT', payload: { type: 'START_TASK', text } },
    (response) => {
      if (chrome.runtime.lastError || !response || !response.ok) {
        showToast('Could not send task — is this a supported portal?');
        return;
      }
      taskInput.value = '';
      showToast('Task sent to KenBot.');
      window.close(); // Close popup so user can see the overlay
    }
  );
}

// ─── Background message listener (refresh on session change) ─────────────────
chrome.runtime.onMessage.addListener((message) => {
  if (message.type === 'SESSION_STARTED' || message.type === 'SESSION_ENDED') {
    refreshStatus();
  }
});

// ─── Toast Utility ────────────────────────────────────────────────────────────

let toastTimer = null;

/**
 * Show a brief notification toast.
 * @param {string} message
 * @param {number} [duration=2500]
 */
function showToast(message, duration = 2500) {
  if (!toastEl) return;
  toastEl.textContent = message;
  toastEl.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.classList.remove('visible');
  }, duration);
}
