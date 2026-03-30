// background.js — MV3 Service Worker
// Responsibilities:
//   - Anon-key management (persistent UUID per browser profile, no login required)
//   - Message routing between popup and content scripts
//   - Tab tracking for active portal sessions
//   - Content script injection on demand
//
// NOTE: MV3 service workers are non-persistent. The WebSocket connection lives
// in content.js (which has a persistent DOM context). The service worker handles
// coordination and anon-key management only.
//
// DEBUG: chrome://extensions → KenBot → "Service worker (Inspect)"
//        Filter by "[KenBot:bg]" to see all background events.

'use strict';

function dbg(...args) { console.debug('[KenBot:bg]', ...args); }

const PORTAL_PATTERNS = [
  'ecitizen.go.ke',
  'ntsa.go.ke',
  'kra.go.ke'
];

// ─── Anon-Key Management ──────────────────────────────────────────────────────

/**
 * Return the persistent anonymous identity UUID for this browser profile.
 * Generated once on first call; stored in chrome.storage.local under
 * 'kenbot_anon_key'. Sent as X-Vault-Key header on all vault API requests.
 * @returns {Promise<string>}
 */
async function getAnonKey() {
  const result = await chrome.storage.local.get('kenbot_anon_key');
  if (result.kenbot_anon_key) return result.kenbot_anon_key;
  const newKey = crypto.randomUUID();
  await chrome.storage.local.set({ kenbot_anon_key: newKey });
  dbg('Generated new anon_key', newKey);
  return newKey;
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

    // Content/popup → background: retrieve persistent anon_key
    case 'GET_ANON_KEY': {
      getAnonKey().then((key) => sendResponse({ key }));
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
    // Pre-generate anon_key so it's ready before any portal page loads
    getAnonKey().then((key) => dbg('Anon key ready:', key));
  }
  if (reason === 'update') {
    dbg('Extension updated.');
  }
});

// Log unhandled errors in the service worker
self.addEventListener('error', (e) => dbg('SW error:', e.message));
self.addEventListener('unhandledrejection', (e) => dbg('SW unhandled rejection:', e.reason));
