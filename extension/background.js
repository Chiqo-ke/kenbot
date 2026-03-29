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

'use strict';

const KENBOT_BACKEND = 'https://your-kenbot-backend.com';
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
  await chrome.storage.local.remove('kenbot_auth_token');
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

    case 'SET_AUTH_TOKEN': {
      setAuthToken(message.token).then(() => sendResponse({ ok: true }));
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
    console.log('[KenBot] Extension installed.');
  }
  if (reason === 'update') {
    console.log('[KenBot] Extension updated.');
  }
});
