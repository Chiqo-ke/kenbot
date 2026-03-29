# ServiceMap Schema Reference

ServiceMaps are JSON files that describe how to automate a specific workflow on a Kenyan government portal. They are built by the Surveyor agent and executed by the Pilot agent.

Every map is validated against the `ServiceMap` Pydantic v2 schema (`backend/maps/schemas.py`) before being written to disk. Invalid maps are rejected.

---

## Full Schema

### `ServiceMap` (root)

```json
{
  "service_id": "ntsa_driving_licence_renewal",
  "service_name": "NTSA — Driving Licence Renewal",
  "portal": "ntsa",
  "version": "1.0.0",
  "last_surveyed": "2025-01-10T14:30:00",
  "surveyor_confidence": 0.92,
  "required_user_data": ["national_id", "ntsa_password", "phone_number"],
  "workflow": [ /* WorkflowStep[] */ ],
  "known_downtimes": ["Saturday 00:00-06:00 EAT"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `service_id` | `string` | ✅ | Unique identifier used as filename and API key. Use snake_case. |
| `service_name` | `string` | ✅ | Human-readable name |
| `portal` | `string` | ✅ | Portal identifier: `"ntsa"`, `"ecitizen"`, `"kra"`, etc. |
| `version` | `string` | ✅ | Semantic version `"MAJOR.MINOR.PATCH"` |
| `last_surveyed` | `string` | ✅ | ISO 8601 datetime when the Surveyor last validated this map |
| `surveyor_confidence` | `float` | ✅ | Confidence score `0.0–1.0`. Below `0.7` triggers auto-healing. |
| `required_user_data` | `string[]` | ✅ | Vault key names needed to execute the workflow |
| `workflow` | `WorkflowStep[]` | ✅ | Ordered list of steps; see below |
| `known_downtimes` | `string[]` | — | Human-readable maintenance windows |

---

### `WorkflowStep`

```json
{
  "step_id": "enter_credentials",
  "step_label": "Enter Login Credentials",
  "url_match": "ntsa.go.ke/login",
  "url_match_strategy": "contains",
  "actions": [ /* Action[] */ ],
  "next_trigger": { /* Selector */ },
  "success_indicator": { /* Selector */ },
  "error_states": [ /* ErrorState[] */ ],
  "requires_human_review": false,
  "estimated_wait_ms": 2000
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `step_id` | `string` | ✅ | Unique within the map. Referenced in WebSocket messages. |
| `step_label` | `string` | ✅ | Shown to user in confirmation dialogs |
| `url_match` | `string` | ✅ | Pattern to match the page URL |
| `url_match_strategy` | `string` | — | `"exact"` \| `"starts-with"` \| `"contains"` \| `"regex"`. Default: `"contains"` |
| `actions` | `Action[]` | ✅ | DOM interactions to perform on this step |
| `next_trigger` | `Selector` | — | Element to click/wait for to advance to the next step |
| `success_indicator` | `Selector` | ✅ | Element that confirms this step completed successfully |
| `error_states` | `ErrorState[]` | — | Known error conditions and recovery actions |
| `requires_human_review` | `boolean` | — | If `true`, Pilot pauses and shows a confirmation dialog before proceeding. Default: `false` |
| `estimated_wait_ms` | `integer` | — | Expected wait time after performing actions (e.g. page load) |

---

### `Action`

```json
{
  "semantic_name": "national_id_field",
  "selector": { /* Selector */ },
  "type": "text",
  "required_data_key": "national_id",
  "placeholder_label": "{{national_id}}",
  "validation_hint": "Must be 8 digits"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `semantic_name` | `string` | ✅ | Descriptive name — used in logs and healing prompts |
| `selector` | `Selector` | ✅ | How to find the element |
| `type` | `string` | ✅ | See Action Types below |
| `required_data_key` | `string \| null` | — | Vault key to fetch and use. If set, the extension retrieves the decrypted value from vault and fills the field. |
| `placeholder_label` | `string \| null` | — | What the Pilot sees instead of the real value: `"{{national_id}}"` |
| `validation_hint` | `string \| null` | — | Shown to user if vault key is missing: `"Must be 8 digits"` |

#### Action Types

| Type | DOM interaction |
|------|----------------|
| `"text"` | `input.value = <value>` (plaintext field) |
| `"password"` | `input.value = <value>` (password field — same interaction, different semantic) |
| `"click"` | `element.click()` |
| `"select"` | `select.value = <value>` |
| `"checkbox"` | `checkbox.checked = true/false` |
| `"file-upload"` | File input (triggers vault file retrieval) |
| `"wait"` | Wait for `selector` to appear — no interaction |

---

### `Selector`

```json
{
  "primary": "[aria-label='National ID Number']",
  "fallbacks": ["[name='id_number']", "#idNumber"],
  "strategy": "aria"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `primary` | `string` | ✅ | Best selector for the element |
| `fallbacks` | `string[]` | — | Tried in order if `primary` fails |
| `strategy` | `string` | ✅ | Strategy used for `primary`; see below |

#### Selector Strategies (priority order)

The Surveyor is instructed to prefer higher-priority strategies. The extension tries `primary` first, then each `fallbacks` entry in order.

| Priority | Strategy | `strategy` value | Example |
|----------|----------|-----------------|---------|
| 1 | ARIA label or role | `"aria"` | `[aria-label='National ID']` |
| 2 | data-testid / data-cy | `"data-attr"` | `[data-testid='id-input']` |
| 3 | name attribute | `"data-attr"` | `[name='id_number']` |
| 4 | XPath | `"xpath"` | `//input[@id='idInput']` |
| 5 | CSS ID or class | `"css"` | `#idNumber` |
| 6 | Text content | `"text-content"` | Button text match |

Never use position-based selectors (`nth-child`, coordinates). They break on minor page re-layouts.

---

### `ErrorState`

```json
{
  "condition": "Invalid KRA PIN message visible",
  "selector": {
    "primary": "[role='alert']",
    "fallbacks": [".error-message"],
    "strategy": "aria"
  },
  "recovery_action": "escalate_to_user"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `condition` | `string` | ✅ | Human description of the error condition |
| `selector` | `Selector` | ✅ | Element that indicates this error is active |
| `recovery_action` | `string` | ✅ | What to do when this error is detected |

#### Recovery Actions

| Action | Behaviour |
|--------|-----------|
| `"retry"` | Re-execute the current step |
| `"restart_workflow"` | Go back to the first step |
| `"escalate_to_user"` | Pause and ask the user what to do |
| `"healing_request"` | Queue a Surveyor task to re-crawl this step |

---

## Complete Example Map

```json
{
  "service_id": "ecitizen_business_registration",
  "service_name": "eCitizen — Business Name Registration",
  "portal": "ecitizen",
  "version": "1.0.0",
  "last_surveyed": "2025-01-10T09:00:00",
  "surveyor_confidence": 0.88,
  "required_user_data": ["national_id", "ecitizen_password", "full_name", "phone_number"],
  "known_downtimes": [],
  "workflow": [
    {
      "step_id": "login",
      "step_label": "Log in to eCitizen",
      "url_match": "ecitizen.go.ke/login",
      "url_match_strategy": "contains",
      "requires_human_review": false,
      "actions": [
        {
          "semantic_name": "national_id_field",
          "type": "text",
          "required_data_key": "national_id",
          "placeholder_label": "{{national_id}}",
          "validation_hint": "Must be 8 digits",
          "selector": {
            "primary": "[aria-label='ID Number']",
            "fallbacks": ["[name='id_no']", "#id_number"],
            "strategy": "aria"
          }
        },
        {
          "semantic_name": "password_field",
          "type": "password",
          "required_data_key": "ecitizen_password",
          "placeholder_label": "{{ecitizen_password}}",
          "selector": {
            "primary": "[aria-label='Password']",
            "fallbacks": ["[name='password']"],
            "strategy": "aria"
          }
        },
        {
          "semantic_name": "login_button",
          "type": "click",
          "required_data_key": null,
          "selector": {
            "primary": "[aria-label='Sign In']",
            "fallbacks": ["button[type='submit']"],
            "strategy": "aria"
          }
        }
      ],
      "success_indicator": {
        "primary": "[aria-label='My Dashboard']",
        "fallbacks": [".dashboard-header"],
        "strategy": "aria"
      },
      "error_states": [
        {
          "condition": "Invalid credentials alert visible",
          "selector": {
            "primary": "[role='alert']",
            "fallbacks": [".alert-danger"],
            "strategy": "aria"
          },
          "recovery_action": "escalate_to_user"
        }
      ],
      "estimated_wait_ms": 3000
    }
  ]
}
```

---

## Storing a Map

### Via API

```http
POST /api/maps/
Authorization: Bearer <admin-jwt>
Content-Type: application/json

{ <ServiceMap JSON> }
```

Invalid maps return `400` with Pydantic validation errors.

### Via Surveyor

Trigger the Surveyor on a URL and it writes/updates the map automatically:

```http
POST /api/surveyor/trigger/
Authorization: Bearer <token>
Content-Type: application/json

{
  "service_id": "ecitizen_business_registration",
  "url": "https://ecitizen.go.ke/login"
}
```

---

## Map File Location

Maps are stored as JSON files in `backend/map_files/` with the filename `{service_id}.json`. The DB model (`maps/models.py`) acts as an index.

To manually inspect a map:

```powershell
Get-Content backend\map_files\ntsa_driving_licence_renewal.json | ConvertFrom-Json | Format-List
```
