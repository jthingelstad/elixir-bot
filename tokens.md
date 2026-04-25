# POAP Tokens API Reference

Base URL: `https://api.poap.tech`

## Authentication

Most endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

Exception: `GET /metadata/{eventId}/{tokenId}` requires no authentication.

## Image Size Options

For `image_url` fields, append a `?size=` query parameter for compressed artwork:

| Size | Dimensions |
|------|-----------|
| `xsmall` | 64x64px |
| `small` | 128x128px |
| `medium` | 256x256px |
| `large` | 512x512px |
| `xlarge` | 1024x1024px |

Example: `https://poap.xyz/image.png?size=small`

Original artwork can be up to 4MB per image.

---

## Endpoints

### GET /actions/scan/{address}/{eventId}

Check if an address holds a POAP for a specific event. Returns a reduced token record with only `event`, `tokenId`, and `owner` if found, or HTTP 404 if not.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `address` | string | Ethereum address, ENS, or email |
| `eventId` | string | Numeric event ID |

**Response** (200): Single [Scan-by-Event object](#scan-by-event-object)

**Response** (404): `{"statusCode": 404, "error": "Not Found", "message": "Address does not have token for this event"}`

---

### GET /actions/scan/{address}

List all POAPs held by an address. Returns 200 with an empty array if the address holds no POAPs.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `address` | string | Ethereum address, ENS, or email |

**Response** (200): Array of [Token objects](#token-object)

---

### GET /event/{id}/poaps

Get paginated info on token holders for a specific event, including token IDs, transfer counts, and owner details.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Numeric event ID |

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | number | 10 | Results per page (max 300) |
| `offset` | number | 0 | Pagination offset |

**Response** (200):

```json
{
  "limit": 10,
  "offset": 0,
  "total": 100,
  "transferCount": 50,
  "tokens": [ /* array of Event Token objects */ ]
}
```

**Event Token object:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | POAP token ID (e.g. `"3010642"`) |
| `created` | string | Time minted (e.g. `"2021-12-23 20:35:10"`) |
| `transferCount` | string | Number of times the POAP has been transferred |
| `owner` | object | Owner details (see below) |

**Owner object:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Owner's Ethereum address |
| `tokensOwned` | number | Total POAPs held by this address |
| `ens` | string | Owner's ENS name (empty string if none) |

---

### GET /metadata/{eventId}/{tokenId}

Get NFT metadata for a specific POAP token. **No authentication required.**

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `eventId` | string | Numeric event ID |
| `tokenId` | string | POAP token ID |

**Response** (200):

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Event name |
| `description` | string | Event description |
| `image_url` | string | POAP image URL |
| `external_url` | string | `https://api.poap.tech/metadata/{eventId}/{tokenId}` |
| `home_url` | string | `https://app.poap.xyz/token/{tokenId}` |
| `year` | number | Event year |
| `tags` | string[] | Array of tags |
| `attributes` | object[] | Array of `{ trait_type, value }` metadata pairs |

---

### GET /token/{tokenId}/image

Get the POAP image for a token. Returns HTTP 302 redirect to the image URL.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `tokenId` | string | POAP token ID |

**Response** (302): Redirect to image URL

---

### GET /token/{tokenId}

Get full details for a specific POAP token including event info, owner, chain layer, and supply.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `tokenId` | string | POAP token ID |

**Response** (200):

| Field | Type | Description |
|-------|------|-------------|
| `event` | [Event](#event-object) | Event details (subset — no `supply` or `drop_image`) |
| `tokenId` | string | POAP token ID (e.g. `"3010642"`) |
| `owner` | string | Owner's Ethereum address |
| `layer` | string | `"Layer1"` (Ethereum) or `"Layer2"` (all other chains: Gnosis, Base, Arbitrum, Polygon, etc.) |
| `created` | string | Time minted (e.g. `"2021-12-08 01:04:55"`) |
| `supply` | object | `{ total: number, order: number }` — total minted and this token's mint order |

---

## Shared Models

### Token Object

Returned by `GET /actions/scan/{address}`.

| Field | Type | Description |
|-------|------|-------------|
| `event` | [Event](#event-object) | Event details (with `supply`, without `drop_image`) |
| `tokenId` | string | POAP token ID (empty string for email-assigned POAPs) |
| `owner` | string | Owner's Ethereum address |
| `chain` | string | Minting chain: `"homestead"` (Ethereum), `"xdai"` (Gnosis), `"base"`, `"matic"` (Polygon), `"arbitrum-one"`, `"celo"`, `"chiliz"`, `"mantle"`, `"unichain"` |
| `created` | string | Time minted (e.g. `"2021-12-08 01:04:55"`) |
| `migrated` | string\|null | Time migrated to another chain, or `null` (may be absent on current responses) |

### Scan-by-Event Object

Returned by `GET /actions/scan/{address}/{eventId}`.

| Field | Type | Description |
|-------|------|-------------|
| `event` | [Scan Event](#scan-event-object) | Event details (without `supply`) |
| `tokenId` | string | POAP token ID |
| `owner` | string | Owner's Ethereum address |

### Event Object

Note: The Event object returned by `GET /actions/scan/{address}` is a subset of the full Event object defined in [events.md](events.md). Fields like `animation_url`, `from_admin`, `virtual_event`, `event_template_id`, `private_event`, and `drop_image` are not returned here. The `supply` field below is unique to this endpoint.

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric event ID (e.g. `16947`) |
| `fancy_id` | string | Unique event identifier from event name (max 256 chars) |
| `name` | string | Event name (max 256 chars) |
| `description` | string | Event description (max 1500 chars) |
| `city` | string | City (optional for virtual events, max 256 chars) |
| `country` | string | Country (optional for virtual events, max 256 chars) |
| `event_url` | string | URL attendees should visit |
| `image_url` | string | POAP image URL (append `?size=small` for lower resolution) |
| `year` | number | Year the event took place |
| `start_date` | string | Start date (returned as `DD-Mon-YYYY`, e.g. `"07-Dec-2021"`) |
| `end_date` | string | End date (returned as `DD-Mon-YYYY`) |
| `expiry_date` | string | Last date POAPs are claimable (returned as `DD-Mon-YYYY`) |
| `timezone` | string | Timezone (e.g. `"America/New_York"`) |
| `supply` | number | Total POAPs minted (present on `GET /actions/scan/{address}` only, not on `/token/{tokenId}`) |

### Scan Event Object

Returned inside [Scan-by-Event object](#scan-by-event-object).

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Numeric event ID (e.g. `16947`) |
| `fancy_id` | string | Unique event identifier from event name (max 256 chars) |
| `name` | string | Event name (max 256 chars) |
| `description` | string | Event description (max 1500 chars) |
| `city` | string | City (optional for virtual events, max 256 chars) |
| `country` | string | Country (optional for virtual events, max 256 chars) |
| `event_url` | string | URL attendees should visit |
| `image_url` | string | POAP image URL (append `?size=small` for lower resolution) |
| `year` | number | Year the event took place |
| `start_date` | string | Start date (returned as `DD-Mon-YYYY`, e.g. `"07-Dec-2021"`) |
| `end_date` | string | End date (returned as `DD-Mon-YYYY`) |
| `expiry_date` | string | Last date POAPs are claimable (returned as `DD-Mon-YYYY`) |
| `timezone` | string | Timezone (e.g. `"America/New_York"`) |

### Drop Image Object

| Field | Type | Description |
|-------|------|-------------|
| `public_id` | string | Image UUID (e.g. `"fc101b69-ca2c-41f4-b65a-09be1ce1de31"`) |
| `drop_id` | number | Drop ID (e.g. `44129`) |
| `gateways` | object[] | Array of image gateway entries |

**Gateway object:**

| Field | Type | Description |
|-------|------|-------------|
| `image_id` | string | Image UUID |
| `filename` | string | Image filename |
| `mime_type` | string | MIME type (e.g. `"image/jpeg"`) |
| `url` | string | Image URL (e.g. `"https://assets.poap.xyz/..."`) |
| `type` | string | `"ORIGINAL"` or `"CROP"` |
