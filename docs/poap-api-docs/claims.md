# POAP Claims API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

**Protected endpoints** (both endpoints in this file) also require an access token:

```
Authorization: Bearer <access-token>
```

Access tokens are valid for 24 hours. See [setup-and-authentication.md](setup-and-authentication.md) for how to generate them.

---

## Endpoints

### POST /actions/claim-qr

Claim a POAP using a previously created mint-link. Retrieve the mint-link's `secret` code first via `GET /actions/claim-qr`.

**Auth:** API key + access token (protected)

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `address` | string | yes | Collector's Ethereum address, ENS, or email |
| `qr_hash` | string | yes | Six-character alphanumeric mint-link code (e.g. `"2abwt1"` from `https://app.poap.xyz/claim/2abwt1`) |
| `secret` | string | yes | Authentication code unique to each mint-link (retrieve via `GET /actions/claim-qr`) |
| `sendEmail` | boolean | no | Whether to send a notification email (default: `true`) |

**Response** (200): [Claim object](#claim-object)

Note: The POST response does not include `tx_hash`, `secret`, `tx_status`, or `result` fields. It includes `transaction_request_id` instead — use `GET /transaction-requests/{id}` to check transaction status.

---

### GET /actions/claim-qr

Look up information on an individual mint-link including claim status, `secret` code, collector (if claimed), and event information.

**Auth:** API key + access token (protected)

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `qr_hash` | string | Six-character alphanumeric mint-link code (e.g. `"2abwt1"`) |

**Response** (200): [Claim object](#claim-object) (field presence varies; see notes below)

---

## Shared Models

### Claim Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric claim ID (e.g. `13894783`) |
| `qr_hash` | string | Six-character alphanumeric mint-link code (e.g. `"2abwt1"`) |
| `event_id` | number | Numeric event ID |
| `beneficiary` | string | Ethereum address or email the collector claimed with. If ENS was used, shows the resolved address |
| `user_input` | string | Raw input the collector used to claim (address, ENS, or email, e.g. `"poap.eth"`) |
| `signer` | string | POAP-owned address that sent the minting transaction |
| `claimed` | boolean | Whether the claim code has been used |
| `claimed_date` | string (datetime) | When the POAP was claimed (e.g. `"2022-07-12T14:22:45.278Z"`) |
| `created_date` | string (datetime) | When the claim codes were created |
| `is_active` | boolean | Whether the claim is active |
| `event` | [Event](#event-object) | The associated event |
| `delegated_mint` | boolean | **Deprecated** |
| `delegated_signed_message` | string\|null | **Deprecated** |

**Additional fields on GET response only:**

| Field | Type | Description |
|-------|------|-------------|
| `tx_hash` | string | Transaction hash (e.g. `"0x238b1f..."`). May be present even when other mint-status fields are empty |
| `secret` | string | Authentication code for this mint-link. On live responses this may also be `"NOT_REQUIRED_ANYMORE"` |
| `tx_status` | string | Minting transaction status. Documented values are `waiting_tx`, `pending`, `passed`, `failed`, or `bumped`, but live responses may also return an empty string |
| `result` | object\|null | Contains `token` (number) when present, but this field is often absent from live responses |

**Additional field on POST response only:**

| Field | Type | Description |
|-------|------|-------------|
| `transaction_request_id` | number | Use with `GET /transaction-requests/{id}` to check transaction status |

### Event Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric event ID (e.g. `16947`) |
| `fancy_id` | string | Unique event identifier generated from event name (max 256 chars, e.g. `"example-event-2022"`) |
| `name` | string | Event name (max 256 chars) |
| `description` | string | Event description (max 1500 chars) |
| `city` | string | City (optional for virtual events, max 256 chars) |
| `country` | string | Country (optional for virtual events, max 256 chars) |
| `event_url` | string | URL attendees should visit |
| `image_url` | string | POAP image URL (append `?size=small` for lower resolution) |
| `animation_url` | string | POAP animation URL |
| `year` | number | Year the event took place |
| `start_date` | string | Start date (returned as `DD-Mon-YYYY`, e.g. `"07-Dec-2021"`) |
| `end_date` | string | End date (returned as `DD-Mon-YYYY`) |
| `expiry_date` | string | Last date POAPs are claimable (returned as `DD-Mon-YYYY`) |
| `timezone` | string | Timezone (e.g. `"America/New_York"`) |
| `location_type` | string | Location type |
| `channel` | string | Channel |
| `platform` | string | Platform |
| `from_admin` | boolean | Whether created by a POAP admin |
| `virtual_event` | boolean | Whether the event is virtual (default: `false`) |
| `event_template_id` | number\|null | Template ID for POAP's claim page (can be `null` or `0`) |
| `private_event` | boolean | If true, filtered from POAP frontend; use for test events (default: `false`) |
