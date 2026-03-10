# POAP Secret Requests API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

`GET /secret/{secret_word}` is a **protected endpoint** and also requires an access token:

```
Authorization: Bearer <access-token>
```

See [setup-and-authentication.md](setup-and-authentication.md) for how to generate access tokens.

## Important Notes

- "word" type is **not compatible** for claiming via API — collectors must use the POAP Mobile app.
- "website" type works via browser. If the POAP Mobile app is installed, it will redirect to the app for minting.
- Use a randomized phrase for the claim name (e.g. `increase-group-student`) and keep the mint window under 10 minutes to prevent guessing.

---

## Endpoints

### GET /secret/{secret_word}

Get information about a specific secret word/website claim and its related event.

**Auth:** API key + access token (protected)

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `secret_word` | string | The `claim_name` entered when creating the claim |

**Response** (200):

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric secret ID |
| `event_id` | number | Associated event ID |
| `event` | [Event object](events.md#event-object) | Full event details |
| `claimed_count` | number | Number of POAPs claimed using this secret |
| `secret` | string | The secret word/website value |
| `active` | boolean | Whether the secret is currently active |
| `type` | string | `"word"` or `"website"` |

---

### POST /secret-requests

Create a new secret word/website claim for an event.

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | number | yes | Numeric event ID |
| `requested_codes` | number | yes | Number of POAP codes for this secret |
| `claim_name` | string | yes | The secret claim name (e.g. `"culture-form-firm"`) |
| `from` | string (datetime) | yes | Valid from date (ISO format: `YYYY-MM-DDTHH:MM:SS.sss`) |
| `to` | string (datetime) | yes | Valid until date (ISO format) |
| `secret_type` | string | yes | `"word"` or `"website"` |
| `secret_code` | string | no | Six-digit edit code (pattern: `^[0-9]{6}$`) |
| `timezone` | number | no | UTC offset (-12 to 12, default: `0`) |
| `active` | boolean | no | Whether the secret is active (default: `true`) |

**Response** (200): `number` (the created secret request ID)

---

### PUT /secret-requests

Update an existing secret word/website claim.

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | number | yes | Numeric event ID |
| `claim_name` | string | yes | The secret claim name |
| `from` | string (datetime) | yes | Valid from date (ISO format) |
| `to` | string (datetime) | yes | Valid until date (ISO format) |
| `active` | boolean | yes | Whether the secret is active |
| `secret_type` | string | yes | `"word"` or `"website"` |
| `secret_code` | string | no | Six-digit edit code |
| `timezone` | number | no | UTC offset (-12 to 12, default: `0`) |
| `accepted_redeems` | number | no | Number of accepted POAP claims for this secret |

**Response** (200): `number` (the updated secret request ID)

---

### POST /website/claim

Claim a POAP using a website-type secret. **Not compatible with "word" type.**

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `website` | string | yes | The `claim_name` of the website claim (e.g. `"super-lizard-hammer"`) |
| `address` | string | yes | Collector's Ethereum address |

**Response** (200): [Claim object](claims.md#claim-object) (GET variant — includes `tx_hash`, `secret`, `tx_status`, `event_template`, `result`)
