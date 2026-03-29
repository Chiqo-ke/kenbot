# Chrome Extension

The KenBot extension is a plain-JavaScript browser extension (Manifest V3) that acts as the thin client for the Pilot agent. All AI reasoning stays on the server — the extension only handles DOM interaction and communication.

---

## Installation (Developer Mode)

1. Open Chrome and navigate to `chrome://extensions`
2. Enable **Developer mode** using the toggle in the top-right corner
3. Click **Load unpacked**
4. Select the `extension/` folder from the repo root
5. The KenBot icon (puzzle piece placeholder until you supply an icon) appears in your toolbar

> To update after making code changes: click the refresh icon on the extension card in `chrome://extensions`, or simply reload the page you're testing on.

---

## Extension Files

| File | Role |
|------|------|
| `manifest.json` | Extension metadata, permissions, service worker declaration |
| `background.js` | Service worker — manages auth state, token storage, coordinates tabs |
| `content.js` | Content script injected into every page — owns the WebSocket connection, mounts the overlay |
| `popup.html` | Toolbar popup HTML — login form and session controls |
| `popup.js` | Popup logic — login, token storage via `chrome.storage.local` |
| `ui/overlay.css` | Styles for the Shadow DOM overlay (fully isolated from page styles) |
| `ui/overlay.js` | Overlay logic — chat bubbles, step confirmations, vault prompts, CAPTCHA notice |

---

## How to Use

### 1 — Log In

Click the KenBot toolbar icon to open the popup. Enter your Django username and password. The popup calls `POST /api/auth/token/` and stores the JWT in `chrome.storage.local`.

### 2 — Start a Session

With the popup open (or via the overlay on any page), click **New Session**. This calls `POST /api/pilot/sessions/` and opens a WebSocket to `ws://localhost:8000/ws/pilot/<session_id>/`.

### 3 — Give a Command

Type a natural-language command in the overlay chat input (English or Swahili):

```
renew my driving licence
```

```
tafuta information ya kampuni TaxPIN yangu
```

### 4 — Follow Prompts

The agent will:
- Ask you to add missing vault credentials if it needs them
- Walk you through each step of the portal workflow
- Pause and show you a summary before submitting any form
- Alert you if a CAPTCHA appears

---

## Vault Management

The vault stores your credentials encrypted on the server. The extension is the only component that ever retrieves and uses the decrypted values — they are injected directly into form fields and never appear in the agent's context.

### Adding a Credential

When a task needs a credential you haven't stored yet, the overlay shows a **"Missing credential"** prompt with an input field. Type the value and click **Save to Vault**. The extension calls `POST /api/vault/` with your JWT.

You can also pre-populate credentials before starting a task:
1. Click the **Vault** tab in the toolbar popup
2. Enter a key name (e.g. `national_id`, `ntsa_password`, `kra_pin`)
3. Enter the value and click **Save**

### Common Vault Keys

| Key | Description |
|-----|-------------|
| `national_id` | Kenyan National ID number |
| `kra_pin` | KRA Personal Identification Number |
| `ntsa_password` | NTSA eCitizen portal password |
| `ecitizen_password` | eCitizen portal password |
| `full_name` | Full legal name as on ID |
| `phone_number` | Registered mobile number |

You can use any key name — the service maps reference keys by name, and the overlay prompts you if a needed key is missing.

### Deleting a Credential

In the popup Vault tab, click the **×** next to a key to delete it (`DELETE /api/vault/<key>/`).

---

## Step Execution

When the agent sends an `execute_step` message the extension:

1. Iterates through the `actions` array
2. For each action with a `required_data_key`:
   - Calls `GET /api/vault/<required_data_key>/` to fetch the decrypted value
   - Writes it directly to the target input element's `.value` — the network call result is never stored
3. For click/select/checkbox actions: performs the DOM interaction
4. Reports `step_confirmed` to the server on success
5. If any selector fails: strips all form values from the current page HTML and reports `step_failed`

---

## CAPTCHA Handling

When the extension detects a CAPTCHA (`<iframe src="*recaptcha*">` or `<div class="g-recaptcha">` on the page):

1. Sends `{"type": "captcha_detected"}` to the server
2. The overlay shows a **"Please solve the CAPTCHA on the page"** notice
3. You solve it manually in the page
4. Click **"CAPTCHA solved"** in the overlay
5. The extension sends `{"type": "captcha_solved"}` and the agent resumes

---

## Permissions

The extension requests the following Chrome permissions:

| Permission | Why |
|------------|-----|
| `storage` | Store JWT and session state via `chrome.storage.local` |
| `activeTab` | Inject content script into the current tab |
| `scripting` | Execute step actions in the page |
| `host_permissions: http://localhost/*` | Communicate with the local Django server (dev) |

In production, `host_permissions` should be updated to your deployed server domain.

---

## Shadow DOM Isolation

The overlay UI (`ui/overlay.js`) is mounted inside a Shadow DOM root attached to a `<div id="kenbot-overlay-root">` element. This means:

- Page styles cannot affect the overlay appearance
- Overlay styles cannot affect the page
- The extension's DOM is invisible to page scripts

---

## Development Tips

### Reload the Extension

After editing any extension file:

```
chrome://extensions → KenBot card → refresh icon
```

Or reload the tab if only `content.js` changed.

### View Extension Logs

- **background.js logs:** In `chrome://extensions`, click **Service Worker** link on the KenBot card
- **content.js logs:** Open DevTools on the target page (F12) → Console
- **popup.js logs:** Right-click the toolbar icon → Inspect popup → Console

### Change Backend URL

The backend URL defaults to `http://localhost:8000`. To point to a different server, update the `BASE_URL` constant in `background.js` and `content.js`.

### Testing Without a Real Portal

Use Django's development mode with `CELERY_TASK_ALWAYS_EAGER=True`. You can send mock WebSocket messages from the browser console:

```javascript
// In the page console, after the extension connects:
// (internals depend on your content.js WS variable name)
window.__kenbotWS.send(JSON.stringify({
  type: "user_message",
  content: "test my ntsa connection"
}));
```
