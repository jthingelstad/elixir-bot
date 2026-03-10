# POAP Redeem Requests API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

`POST /redeem-requests` is a **protected endpoint** and also requires an access token:

```
Authorization: Bearer <access-token>
```

See [setup-and-authentication.md](setup-and-authentication.md) for how to generate access tokens.

---

## Endpoints

### GET /redeem-requests/active/count

Get the number of active redeem requests for an event.

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `event_id` | number | Numeric event ID (e.g. `16947`) |
| `redeem_type` | string | **Required.** Redeem method: `qr_code`, `secret_website`, or `secret_word`. Omitting this parameter returns a 503 error |

**Response** (200): Array of objects. Returns an empty array `[]` when there are no active requests.

When active requests exist, each object contains:

| Field | Type | Description |
|-------|------|-------------|
| `active` | number | Count of active redeem requests |
| `type` | string | Redeem method type: `secret_word`, `secret_website`, or `qr_code` |

---

### POST /redeem-requests

Request additional codes for a redeem method (QR Code, Secret Website, or Secret Word).

**Auth:** API key + access token (protected)

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | number | yes | Numeric event ID |
| `requested_codes` | number | yes | Number of codes needed (max 50,000) |
| `redeem_type` | string | yes | Redeem method: `qr_code`, `secret_website`, or `secret_word` |
| `secret_code` | string | no | Six-digit edit code (pattern: `^[0-9]{6}$`) |
| `notify_issuer` | boolean | no | If false, suppresses mint-link and edit code emails (default: `true`) |

**Response** (200):

```json
{ "id": "string" }  // Petition ID to track approval status
```
