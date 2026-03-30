// background.js — MV3 Service Worker
// Responsibilities:
//   - Auth token management (store/retrieve JWT from chrome.storage.local)
//   - Message routing between popup and content scripts
//   - Tab tracking for active portal sessions
//   - Content script injection on demand
//
// NOTE: MV3 service workers are non-persistent. The WebSocket connection lives
// in content.js (which has a persistent DOM context). The service worker handles
// coordination and token management only.
//
// DEBUG: chrome://extensions → KenBot → "Service worker (Inspect)"
//        Filter by "[KenBot:bg]" to see all background events.

'use strict';

function dbg(...args) { console.debug('[KenBot:bg]', ...args); }

const KENBOT_BACKEND = 'http://127.0.0.1:8000';
const PORTAL_PATTERNS = [
  'ecitizen.go.ke',
  'ntsa.go.ke',
  'kra.go.ke'
];

// ─── Auth Token Management ────────────────────────────────────────────────────

/**
 * Retrieve the stored JWT. Never exposes vault values — only auth tokens.
 * @returns {Promise<string|null>}
 */
async function getAuthToken() {
  const result = await chrome.storage.local.get('kenbot_auth_token');
  return result.kenbot_auth_token || null;
}

/**
 * Persist a JWT received from the backend login endpoint.
 * Token is stored encrypted-at-rest by the OS (chrome.storage.local is sandboxed).
 * @param {string} token
 */
async function setAuthToken(token) {
  await chrome.storage.local.set({ kenbot_auth_token: token });
}

/**
 * Clear auth state on logout.
 */
async function clearAuthToken() {
  await chrome.storage.local.remove(['kenbot_auth_token', 'kenbot_refresh_token']);
}

async function getRefreshToken() {
  const result = await chrome.storage.local.get('kenbot_refresh_token');
  return result.kenbot_refresh_token || null;
}

async function setRefreshToken(token) {
  await chrome.storage.local.set({ kenbot_refresh_token: token });
}

// ─── Session State ────────────────────────────────────────────────────────────
// Maps tabId → sessionId so the popup can show the right session status.
const activeSessions = new Map();

// ─── Message Router ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const { type } = message;

  switch (type) {
    // Popup → background: check whether KenBot is active on the current tab
    case 'GET_SESSION_STATUS': {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs[0];
        if (!tab) { sendResponse({ active: false }); return; }
        sendResponse({
          active: activeSessions.has(tab.id),
          sessionId: activeSessions.get(tab.id) || null,
          tabId: tab.id
        });
      });
      return true; // keep message channel open for async sendResponse
    }

    // Content script → background: register a new session
    case 'SESSION_STARTED': {
      dbg('Session started tabId=%d sessionId=%s', sender.tab && sender.tab.id, message.sessionId);
      if (sender.tab) {
        activeSessions.set(sender.tab.id, message.sessionId);
        broadcastToPopup({ type: 'SESSION_STARTED', sessionId: message.sessionId });
      }
      sendResponse({ ok: true });
      break;
    }

    // Content script → background: session ended or WS disconnected
    case 'SESSION_ENDED': {
      if (sender.tab) {
        activeSessions.delete(sender.tab.id);
        broadcastToPopup({ type: 'SESSION_ENDED' });
      }
      sendResponse({ ok: true });
      break;
    }

    // Popup → background: get/set auth token
    case 'GET_AUTH_TOKEN': {
      getAuthToken().then((token) => sendResponse({ token }));
      return true;
    }

    case 'REFRESH_ACCESS_TOKEN': {
      getRefreshToken().then(async (refresh) => {
        if (!refresh) { sendResponse({ ok: false, reason: 'no_refresh_token' }); return; }
        try {
          const res = await fetch(`${KENBOT_BACKEND}/api/auth/token/refresh/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh })
          });
          if (!res.ok) {
            dbg('Token refresh failed HTTP %d', res.status);
            await clearAuthToken();
            sendResponse({ ok: false, reason: 'refresh_failed' });
            return;
          }
          const data = await res.json();
          await setAuthToken(data.access);
          if (data.refresh) await setRefreshToken(data.refresh);
          dbg('Access token refreshed successfully');
          sendResponse({ ok: true });
        } catch (err) {
          dbg('Token refresh network error:', err);
          sendResponse({ ok: false, reason: err.message });
        }
      });
      return true;
    }

    case 'SET_AUTH_TOKEN': {
      const storeOps = [setAuthToken(message.token)];
      if (message.refreshToken) storeOps.push(setRefreshToken(message.refreshToken));
      Promise.all(storeOps).then(() => {
        // Notify all portal tabs so their content scripts can connect WS
        chrome.tabs.query({}, (tabs) => {
          tabs.forEach((tab) => {
            if (tab.id && PORTAL_PATTERNS.some((p) => tab.url && tab.url.includes(p))) {
              chrome.tabs.sendMessage(tab.id, { type: 'AUTH_TOKEN_SET' }).catch(() => {});
            }
          });
        });
        sendResponse({ ok: true });
      });
      return true;
    }

    case 'CLEAR_AUTH_TOKEN': {
      clearAuthToken().then(() => sendResponse({ ok: true }));
      return true;
    }

    // Popup → content script: relay a user command (e.g. start task)
    case 'RELAY_TO_CONTENT': {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs[0];
        if (!tab) { sendResponse({ ok: false, error: 'No active tab' }); return; }
        chrome.tabs.sendMessage(tab.id, message.payload, (response) => {
          sendResponse(response || { ok: true });
        });
      });
      return true;
    }

    // Content script status update → relay to popup
    case 'STATUS_UPDATE': {
      broadcastToPopup(message);
      break;
    }

    default:
      break;
  }
});

// ─── Tab Cleanup ──────────────────────────────────────────────────────────────
// Remove stale session state when a portal tab is closed.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (activeSessions.has(tabId)) {
    activeSessions.delete(tabId);
    broadcastToPopup({ type: 'SESSION_ENDED' });
  }
});

// ─── Popup Broadcast ─────────────────────────────────────────────────────────
/**
 * Send a message to the popup if it is open.
 * chrome.runtime.sendMessage will throw if nothing is listening — swallow it.
 * @param {object} payload
 */
function broadcastToPopup(payload) {
  chrome.runtime.sendMessage(payload).catch(() => {
    // Popup is closed — silently discard
  });
}

// ─── Install / Update Lifecycle ──────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === 'install') {
    dbg('Extension installed.');
  }
  if (reason === 'update') {
    dbg('Extension updated.');
  }
});

// Log unhandled errors in the service worker
self.addEventListener('error', (e) => dbg('SW error:', e.message));
self.addEventListener('unhandledrejection', (e) => dbg('SW unhandled rejection:', e.reason));
