# POAP Delivery API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key passed via header:

```
x-api-key: <your-api-key>
```

---

## Endpoints

### POST /actions/claim-delivery-v2

Claim a POAP from a delivery.

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | number | yes | The numeric ID of the delivery |
| `address` | string | yes | Collector's Ethereum address or ENS (e.g. `0x19C234364C70E45287B631BAA04e42BA58173f54`) |

**Response** (200):

```json
{
  "queue_uid": "string"  // UUID to check minting status
}
```

---

### POST /deliveries

Create a new delivery.

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | yes | URL-safe identifier for `https://poap.delivery/{slug}` |
| `card_title` | string | yes | Title shown in card view on poap.delivery homepage |
| `card_text` | string | yes | Description shown in card view on poap.delivery homepage |
| `page_title` | string | yes | Title shown on the delivery page |
| `page_title_image` | string | yes | Image URL shown alongside title on delivery page |
| `page_text` | string | yes | Description shown on the delivery page |
| `event_ids` | string | yes | Numeric drop ID (pattern: `^[1-9][0-9]*$`) |
| `secret_codes` | string | yes | Six-digit edit code set at event creation (pattern: `^[0-9]{6}$`, e.g. `"234789"`) |
| `image` | string | yes | Image URL for card and page views |
| `metadata_title` | string | yes | Title for social card previews when sharing the link |
| `metadata_description` | string | yes | Description for social card previews when sharing the link |
| `addresses` | array | yes | List of address objects (min 1 item) |

**`addresses` array items:**

| Field | Type | Description |
|-------|------|-------------|
| `address` | string | Collector's Ethereum address or ENS |
| `events` | number[] | Array of event IDs for this address |

**Response** (200): [Delivery object](#delivery-object)

---

### GET /deliveries

List all deliveries, paginated in descending order by ID.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | number | 10 | Results per page (max 1000) |
| `offset` | number | 0 | Pagination offset |
| `event_id` | number | — | Filter by event ID |
| `approved` | boolean | — | Filter by approval status (only `true` supported) |
| `address` | string | — | Filter by collector's Ethereum address or ENS |

**Response** (200):

```json
{
  "limit": 10,
  "offset": 0,
  "total": 100,
  "deliveries": [ /* array of Delivery objects */ ]
}
```

---

### GET /delivery/{id}

Get a delivery by its numeric ID.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | The numeric ID of the delivery |

**Response** (200): [Delivery object](#delivery-object)

---

### GET /delivery/slug/{slug}

Get a delivery by its URL-safe slug.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `slug` | string | The slug portion of `https://poap.delivery/{slug}` |

**Response** (200): [Delivery object](#delivery-object)

---

### GET /delivery-addresses/{id}/address/{address}

Get the delivery claim status for a specific address, including event details.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | The numeric ID of the delivery |
| `address` | string | Collector's Ethereum address or ENS |

**Response** (200): [Delivery Address object](#delivery-address-object) (with nested [Event objects](#event-object))

---

### GET /delivery-addresses/{id}

List all addresses eligible to claim a delivery, paginated.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | The numeric ID of the delivery |

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | number | 10 | Results per page (max 1000) |
| `offset` | number | 0 | Pagination offset |
| `claimed` | boolean | — | Filter by claim status |

**Response** (200):

```json
{
  "limit": 10,
  "offset": 0,
  "total": 100,
  "items": [ /* array of Delivery Address objects */ ]
}
```

---

## Shared Models

### Delivery Object

Note: The fields returned vary by endpoint. See notes below.

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric delivery ID |
| `slug` | string | URL-safe identifier for `https://poap.delivery/{slug}` |
| `card_title` | string | Title for card view on poap.delivery homepage |
| `card_text` | string | Description for card view on poap.delivery homepage |
| `page_title` | string | Title on the delivery page |
| `page_title_image` | string | Image URL alongside the title on the delivery page |
| `page_text` | string | Description on the delivery page |
| `metadata_title` | string | Title for social card previews |
| `metadata_description` | string | Description for social card previews |
| `event_ids` | string | Numeric drop ID (pattern: `^[1-9][0-9]*$`) |
| `image` | string | Image URL for card and page views |
| `active` | boolean | Whether the delivery is claimable (via website or API) |
| `approved` | boolean | Whether the delivery has been approved by the Curation Body |
| `claimed_addresses` | number | Count of addresses that have claimed (on `GET /delivery/{id}` only) |
| `total_addresses` | number | Total eligible addresses (on `GET /delivery/{id}` only) |
| `private` | boolean | If true, hidden from poap.delivery homepage but still accessible via direct slug URL (on `GET /deliveries` only) |

### Delivery Address Object

| Field | Type | Description |
|-------|------|-------------|
| `address` | string | Collector's Ethereum address or ENS |
| `claimed` | boolean | Whether this address has claimed the delivery |
| `event_ids` | string | Numeric drop ID |
| `events` | Event[] | Array of events the address can claim (only present on `GET /delivery-addresses/{id}/address/{address}`, not on the paginated listing) |

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
| `start_date` | string | Start date (returned as ISO 8601 timestamp, e.g. `"2026-02-16T06:00:00.000Z"`) |
| `end_date` | string | End date (returned as ISO 8601 timestamp) |
| `expiry_date` | string | Last date POAPs are claimable (returned as ISO 8601 timestamp) |
| `timezone` | string | Timezone (e.g. `"America/New_York"`) |
| `location_type` | string | Location type |
| `channel` | string | Channel |
| `platform` | string | Platform |
| `from_admin` | boolean | Whether created by a POAP admin |
| `virtual_event` | boolean | Whether the event is virtual (default: `false`) |
| `event_template_id` | number\|null | Template ID for POAP's claim page (can be `null` or `0`) |
| `private_event` | boolean | If true, filtered from POAP frontend; use for test events (default: `false`) |
