# API Reference

All REST endpoints require a JWT access token unless noted. The WebSocket endpoint uses the same JWT via query parameter or header.

---

## Authentication

### Obtain Token

```http
POST /api/auth/token/
Content-Type: application/json

{
  "username": "your-username",
  "password": "your-password"
}
```

**Response `200`:**

```json
{
  "access": "<jwt-access-token>",
  "refresh": "<jwt-refresh-token>"
}
```

Access tokens expire in 5 minutes by default. Use the refresh token to get a new one.

### Refresh Token

```http
POST /api/auth/token/refresh/
Content-Type: application/json

{
  "refresh": "<jwt-refresh-token>"
}
```

**Response `200`:**

```json
{
  "access": "<new-jwt-access-token>"
}
```

### Using the Token

All subsequent requests must include:

```http
Authorization: Bearer <jwt-access-token>
```

---

## Pilot Sessions

### List Sessions

```http
GET /api/pilot/sessions/
Authorization: Bearer <token>
```

**Response `200`:**

```json
[
  {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "active",
    "created_at": "2025-01-15T10:30:00Z"
  }
]
```

### Create Session

Creates a new session ID to use when opening a WebSocket connection.

```http
POST /api/pilot/sessions/
Authorization: Bearer <token>
```

**Response `201`:**

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "active",
  "created_at": "2025-01-15T10:30:00Z"
}
```

### Get Session Detail

```http
GET /api/pilot/sessions/<session_id>/
Authorization: Bearer <token>
```

**Response `200`** — session object. **`404`** if session belongs to another user.

### Get Session Logs

```http
GET /api/pilot/sessions/<session_id>/logs/
Authorization: Bearer <token>
```

**Response `200`:**

```json
[
  {
    "id": 1,
    "role": "user",
    "content": "renew my driving licence",
    "timestamp": "2025-01-15T10:30:05Z"
  },
  {
    "id": 2,
    "role": "agent",
    "content_en": "I'll help you renew your driving licence on NTSA.",
    "content_sw": "Nitakusaidia kuhuisha leseni yako ya udereva kwenye NTSA.",
    "timestamp": "2025-01-15T10:30:06Z"
  }
]
```

---

## Maps

### List Service Maps

```http
GET /api/maps/
Authorization: Bearer <token>
```

**Response `200`:**

```json
[
  {
    "service_id": "ntsa_driving_licence_renewal",
    "display_name": "NTSA — Driving Licence Renewal",
    "portal_url": "https://ntsa.go.ke/...",
    "version": "2025-01-10",
    "active": true
  }
]
```

### Get Map Detail

```http
GET /api/maps/<service_id>/
Authorization: Bearer <token>
```

**Response `200`** — full `ServiceMap` JSON. See [docs/maps.md](maps.md) for schema.

### Create / Update Map (admin)

```http
POST /api/maps/
Authorization: Bearer <token>
Content-Type: application/json

{ <ServiceMap JSON> }
```

The body must pass `ServiceMap.model_validate()` — invalid maps are rejected with `400` and validation errors.

---

## Vault

### Store a Credential

```http
POST /api/vault/
Authorization: Bearer <token>
Content-Type: application/json

{
  "key": "national_id",
  "value": "12345678"
}
```

The value is AES-256-GCM encrypted before storage. The plaintext is never stored or logged.

**Response `201`:** `{ "key": "national_id", "stored": true }`

### Retrieve a Credential (decrypted)

```http
GET /api/vault/<key>/
Authorization: Bearer <token>
```

Returns the decrypted plaintext. This endpoint is called by the Chrome extension to inject values into the DOM. It should only be called over HTTPS in production.

**Response `200`:** `{ "key": "national_id", "value": "12345678" }`  
**Response `404`:** key not found for this user.

### Delete a Credential

```http
DELETE /api/vault/<key>/
Authorization: Bearer <token>
```

**Response `204`:** No content.

---

## Surveyor

### Trigger a Survey

Starts a background Celery task to crawl a portal and build/update a ServiceMap.

```http
POST /api/surveyor/trigger/
Authorization: Bearer <token>
Content-Type: application/json

{
  "service_id": "ntsa_driving_licence_renewal",
  "url": "https://ntsa.go.ke/driving-licence/renew"
}
```

**Response `202`:**

```json
{
  "job_id": "ntsa_driving_licence_renewal",
  "status": "queued"
}
```

### List Survey Jobs

```http
GET /api/surveyor/jobs/
Authorization: Bearer <token>
```

### Get Survey Job Status

```http
GET /api/surveyor/jobs/<service_id>/
Authorization: Bearer <token>
```

**Response `200`:**

```json
{
  "service_id": "ntsa_driving_licence_renewal",
  "status": "completed",
  "queued_at": "2025-01-15T09:00:00Z",
  "completed_at": "2025-01-15T09:02:30Z",
  "error": null
}
```

Status values: `queued` | `running` | `completed` | `failed`

---

## WebSocket Protocol

### Connection

```
ws://localhost:8000/ws/pilot/<session_id>/
```

The session must exist (created via `POST /api/pilot/sessions/`) and belong to the authenticated user. Authentication is via JWT sent as a query parameter or in the `Authorization` header during the HTTP upgrade handshake.

Unauthenticated connections are closed with code **4001**.  
Session not found: code **4004**.

---

### Extension → Server Messages

All messages are JSON objects with a `"type"` field.

#### `user_message`

User typed a natural-language command.

```json
{
  "type": "user_message",
  "content": "renew my driving licence"
}
```

#### `step_confirmed`

User confirmed the pre-submission review and the extension may proceed.

```json
{
  "type": "step_confirmed",
  "step_id": "confirm_details"
}
```

#### `step_failed`

A selector failed in the extension — page may have changed.

```json
{
  "type": "step_failed",
  "step_id": "enter_id_number",
  "selector": "[name='id_number']",
  "page_context": "<html>...</html>"
}
```

> `page_context` must have all form `value` attributes stripped before sending.

#### `captcha_detected`

Extension detected a CAPTCHA challenge.

```json
{
  "type": "captcha_detected"
}
```

#### `captcha_solved`

User solved the CAPTCHA manually.

```json
{
  "type": "captcha_solved"
}
```

#### `vault_key_added`

User added a missing credential to the vault.

```json
{
  "type": "vault_key_added",
  "vault_key": "national_id"
}
```

#### `confirmation_response`

User responded to a confirmation dialog.

```json
{
  "type": "confirmation_response",
  "confirmed": true
}
```

---

### Server → Extension Messages

#### `agent_message`

Conversational message from the agent. Bilingual.

```json
{
  "type": "agent_message",
  "content_en": "I'll help you renew your driving licence.",
  "content_sw": "Nitakusaidia kuhuisha leseni yako ya udereva."
}
```

#### `execute_step`

Instruction for the extension to perform DOM actions.

```json
{
  "type": "execute_step",
  "step_id": "enter_id_number",
  "actions": [
    {
      "semantic_name": "national_id_field",
      "selector": {
        "primary": "[aria-label='National ID']",
        "fallbacks": ["[name='id_number']", "#id_number"],
        "strategy": "aria"
      },
      "type": "text",
      "required_data_key": "national_id",
      "placeholder_label": "{{national_id}}"
    }
  ]
}
```

When `required_data_key` is set, the extension calls `GET /api/vault/<required_data_key>/` to get the plaintext and types it into the field.

#### `pause_confirmation`

Agent wants to pause for user review before submitting.

```json
{
  "type": "pause_confirmation",
  "step_label": "Submit Renewal Application",
  "fields": "Name: John Doe\nID: ****7890\nFee: KES 3,000"
}
```

#### `await_captcha`

Agent is paused waiting for the user to solve a CAPTCHA.

```json
{
  "type": "await_captcha"
}
```

#### `await_vault_key`

One or more credentials are missing from the vault.

```json
{
  "type": "await_vault_key",
  "missing_keys": ["national_id", "ntsa_password"]
}
```

#### `state_update`

Periodic session state snapshot.

```json
{
  "type": "state_update",
  "state": {
    "status": "executing",
    "current_step": "enter_id_number",
    "service_id": "ntsa_driving_licence_renewal"
  }
}
```

#### `error`

Non-fatal error — session continues.

```json
{
  "type": "error",
  "message": "Selector failed for step enter_id_number. Healing queued."
}
```

#### `session_complete`

Task finished successfully.

```json
{
  "type": "session_complete"
}
```

---

## Error Codes Summary

| HTTP Code | Meaning |
|-----------|---------|
| 400 | Validation error (check `detail` field) |
| 401 | Missing or invalid JWT |
| 403 | Authenticated but not authorized (e.g. other user's session) |
| 404 | Resource not found |
| 202 | Accepted — async task queued |

| WS Close Code | Meaning |
|---------------|---------|
| 4001 | Unauthenticated |
| 4004 | Session not found |
