# POAP Events API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

**Protected endpoints** (`POST /event/validate`, `POST /event/{id}/qr-codes`) also require an access token:

```
Authorization: Bearer <access-token>
```

Access tokens are valid for 24 hours. See [setup-and-authentication.md](setup-and-authentication.md) for how to generate them.

---

## Endpoints

### POST /event/validate

Check if a secret code (edit code) is valid for an event.

**Auth:** API key + access token (protected)

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_id` | number | yes | Numeric event ID |
| `secret_code` | string | yes | Six-digit edit code (pattern: `^[0-9]{6}$`) |

**Response** (200):

```json
{ "valid": true }
```

---

### POST /event/{id}/qr-codes

Get the list of mint-link `qr_hash` codes for an event with their claim status.

**Auth:** API key + access token (protected)

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Numeric event ID |

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `secret_code` | string | yes | Six-digit edit code (pattern: `^[0-9]{6}$`) |

**Response** (200):

```json
[
  { "qr_hash": "2abwt1", "claimed": false },
  { "qr_hash": "x9k3m2", "claimed": true }
]
```

---

### POST /events

Create a new event. An email confirmation is sent on creation, and a follow-up on approval/decline with mint-link codes.

See [quality guidelines](https://documentation.poap.tech/docs/integration-quality-requirements) for responsible event creation.

**Testing:** Add "test" to artwork and title, mark as private, request no more than 10 mint links.

**Content-Type:** `multipart/form-data`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Event name (max 256 chars) |
| `description` | string | yes | Event description (max 1500 chars) |
| `city` | string | yes | City (optional for virtual events, max 256 chars) |
| `country` | string | yes | Country (optional for virtual events, max 256 chars) |
| `start_date` | string | yes | Start date (`YYYY-MM-DD` or `MM-DD-YYYY`) |
| `end_date` | string | yes | End date (`YYYY-MM-DD` or `MM-DD-YYYY`) |
| `expiry_date` | string | yes | Last claimable date (`YYYY-MM-DD` or `MM-DD-YYYY`) |
| `virtual_event` | boolean | yes | Whether the event is virtual |
| `image` | binary | yes | POAP image (GIF, PNG, WebP; max 4MB) |
| `email` | string | yes | Issuer's email address (max 256 chars) |
| `secret_code` | string | yes | Six-digit edit code (pattern: `^[0-9]{6}$`) |
| `event_url` | string | no | URL attendees should visit |
| `event_template_id` | number | no | Claim page template ID (default: `1`) |
| `requested_codes` | number | no | Number of mint-link codes requested |
| `private_event` | boolean | no | If true, hidden from POAP frontend (default: `false`) |
| `notify_issuer` | boolean | no | If false, suppresses confirmation emails (default: `true`) |

**Response** (200): [Event object](#event-object)

---

### GET /events/id/{id}

Get event details by numeric event ID.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Numeric event ID |

**Response** (200): [Event object](#event-object) (`drop_image` may be present; older events may omit it)

---

### PUT /events/{fancyId}

Update an event's details. The image cannot be updated.

**Note:** Events can only be edited within 30 days of the start date.

**Content-Type:** `multipart/form-data`

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `fancyId` | string | The event's unique fancy_id string |

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Event name (max 256 chars) |
| `description` | string | yes | Event description (max 1500 chars) |
| `city` | string | yes | City |
| `country` | string | yes | Country |
| `start_date` | string | yes | Start date (`YYYY-MM-DD` or `MM-DD-YYYY`) |
| `end_date` | string | yes | End date (`YYYY-MM-DD` or `MM-DD-YYYY`) |
| `expiry_date` | string | yes | Last claimable date |
| `event_url` | string | yes | URL attendees should visit |
| `secret_code` | string | yes | Six-digit edit code |
| `virtual_event` | boolean | no | Whether the event is virtual (default: `false`) |
| `private_event` | boolean | no | If true, hidden from POAP frontend (default: `false`) |
| `event_template_id` | number | no | Claim page template ID (default: `1`) |

**Response** (204): Empty (success)

---

### GET /events/{fancyId}

Get event details by fancy ID string.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `fancyId` | string | The event's unique fancy_id string |

**Response** (200): [Event object](#event-object) (`drop_image` may be present; older events may omit it)

---

### GET /paginated-events

List events in descending order by start date, paginated.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | number | 10 | Results per page (max 1000) |
| `offset` | number | 0 | Pagination offset |
| `name` | string | — | Filter events by name (partial match) |

**Response** (200):

```json
{
  "items": [ /* array of Event objects (with drop_image) */ ],
  "total": 100,
  "offset": 0,
  "limit": 10
}
```

---

## Shared Models

### Event Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric event ID (e.g. `16947`) |
| `fancy_id` | string | Unique event identifier from event name (max 256 chars, e.g. `"example-event-2022"`) |
| `name` | string | Event name (max 256 chars) |
| `description` | string | Event description (max 1500 chars) |
| `city` | string | City (optional for virtual events, max 256 chars) |
| `country` | string | Country (optional for virtual events, max 256 chars) |
| `event_url` | string | URL attendees should visit |
| `image_url` | string | POAP image URL (append `?size=small` for lower resolution) |
| `animation_url` | string | POAP animation URL (present on `/paginated-events`, absent on `/events/id` and `/events/{fancyId}`) |
| `year` | number | Year the event took place |
| `start_date` | string | Start date (returned as `DD-Mon-YYYY`, e.g. `"07-Dec-2021"`) |
| `end_date` | string | End date (returned as `DD-Mon-YYYY`) |
| `expiry_date` | string | Last date POAPs are claimable (returned as `DD-Mon-YYYY`) |
| `timezone` | string | Timezone (e.g. `"America/New_York"`) |
| `location_type` | string | Location type (undocumented; present on responses) |
| `channel` | string | Channel (undocumented; present on responses) |
| `platform` | string | Platform (undocumented; present on responses) |
| `created_date` | string | ISO 8601 creation timestamp (present on `/events/id` and `/events/{fancyId}`, absent on `/paginated-events`) |
| `from_admin` | boolean | Whether created by a POAP admin |
| `virtual_event` | boolean | Whether the event is virtual (default: `false`) |
| `event_template_id` | number\|null | Template ID for POAP's claim page (can be `null` or `0`) |
| `private_event` | boolean | If true, filtered from POAP frontend (default: `false`) |
| `secret_code` | string | Six-digit edit code (only on some responses) |
| `email` | string | Issuer's email (only on some responses) |
| `drop_image` | [Drop Image](#drop-image-object) | Drop image details (on GET responses; may be absent on older events) |

### Drop Image Object

| Field | Type | Description |
|-------|------|-------------|
| `public_id` | string | Image UUID |
| `drop_id` | number | Drop ID |
| `gateways` | object[] | Array of image gateway entries |

**Gateway object:**

| Field | Type | Description |
|-------|------|-------------|
| `image_id` | string | Image UUID |
| `filename` | string | Image filename |
| `mime_type` | string | MIME type (e.g. `"image/jpeg"`) |
| `url` | string | Image URL (e.g. `"https://assets.poap.xyz/..."`) |
| `type` | string | `"ORIGINAL"` or `"CROP"` |
