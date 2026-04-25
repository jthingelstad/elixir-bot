# POAP API Documentation Index

This directory contains the complete reference for the POAP API. Start here when building an integration.

## Quick Start

- **Base URL:** `https://api.poap.tech`
- **Auth setup:** [setup-and-authentication.md](setup-and-authentication.md) — get API keys, generate access tokens
- **Common workflows:** [guide-common-flows.md](guide-common-flows.md) — mint POAPs, display collections, token gating
- **Create a POAP end-to-end:** [guide-create-poap.md](guide-create-poap.md) — create drop, choose distribution, mint

## Authentication Summary

Most endpoints require an API key:

```
x-api-key: <your-api-key>
```

Exception: `GET /metadata/{eventId}/{tokenId}` requires no authentication.

**Protected endpoints** additionally require a bearer access token (valid 24 hours, generated via OAuth2 client credentials at `https://auth.accounts.poap.xyz/oauth/token`):

```
Authorization: Bearer <access-token>
```

Protected endpoints:
- `POST /actions/claim-qr` — Mint a POAP via mint link
- `GET /actions/claim-qr` — Get mint link status and secret
- `POST /event/validate` — Validate a secret code
- `POST /event/{id}/qr-codes` — Get mint-link codes for an event
- `POST /redeem-requests` — Request additional mint codes
- `GET /secret/{secret_word}` — Get secret word/website claim info

All other documented endpoints in this repo, except `GET /metadata/{eventId}/{tokenId}`, are open (API key only).

## API Reference Files

### Core Actions
| File | Endpoints | Description |
|------|-----------|-------------|
| [claims.md](claims.md) | `POST /actions/claim-qr`, `GET /actions/claim-qr` | Claim POAPs via mint links; look up mint-link status |
| [tokens.md](tokens.md) | `GET /actions/scan/{address}`, `GET /actions/scan/{address}/{eventId}`, `GET /event/{id}/poaps`, `GET /metadata/{eventId}/{tokenId}`, `GET /token/{tokenId}`, `GET /token/{tokenId}/image` | Look up POAPs by address, event, or token ID; get metadata |
| [events.md](events.md) | `POST /events`, `GET /events/id/{id}`, `GET /events/{fancyId}`, `PUT /events/{fancyId}`, `POST /event/validate`, `POST /event/{id}/qr-codes`, `GET /paginated-events` | Create, read, update events; validate codes; list mint links |

### Distribution Methods
| File | Endpoints | Description |
|------|-----------|-------------|
| [delivery.md](delivery.md) | `POST /actions/claim-delivery-v2`, `POST /deliveries`, `GET /deliveries`, `GET /delivery/{id}`, `GET /delivery/slug/{slug}`, `GET /delivery-addresses/{id}/address/{address}`, `GET /delivery-addresses/{id}` | Whitelist-based distribution to specific addresses |
| [secret-requests.md](secret-requests.md) | `GET /secret/{secret_word}`, `POST /secret-requests`, `PUT /secret-requests`, `POST /website/claim` | Secret Word and Website distribution methods |
| [redeem-requests.md](redeem-requests.md) | `GET /redeem-requests/active/count`, `POST /redeem-requests` | Request additional codes for any distribution method |

### Infrastructure
| File | Endpoints | Description |
|------|-----------|-------------|
| [queues.md](queues.md) | `GET /transaction-requests/{id}` | Check minting transaction status |
| [utils.md](utils.md) | `GET /health-check`, `POST /drops/{dropId}/minting-config`, `PATCH /drops/{dropId}/minting-config` | Health check; get/set minting blockchain |
| [qr-code-generate.md](qr-code-generate.md) | `POST /qr-code/generate` | QR code generation (undocumented schema) |

### Reference
| File | Description |
|------|-------------|
| [errors.md](errors.md) | Error codes by HTTP status (400, 401, 403, 404, 500) |
| [smart-contract-reference.md](smart-contract-reference.md) | Contract address and chain deployments |

## Common Workflow: Mint a POAP to a Wallet

```
1. POST /event/{id}/qr-codes     → get qr_hash codes (needs secret_code)
2. GET  /actions/claim-qr?qr_hash=xxx  → get the mint-link secret
3. POST /actions/claim-qr        → mint with { address, qr_hash, secret }
4. GET  /transaction-requests/{id}     → poll until status = "FINISH"
```

## Common Workflow: Check if an Address Holds a POAP

```
GET /actions/scan/{address}/{eventId}  → returns { event, tokenId, owner } (200) or 404 if not held
```

## Gotchas and Known Quirks

### Content Types
- `POST /events` and `PUT /events/{fancyId}` require `multipart/form-data` (not JSON) because they include a binary `image` field.
- All other POST/PUT/PATCH endpoints use `application/json`.

### The Edit Code / Secret Code
The six-digit code created with an event is called different names in different endpoints:
- `secret_code` (string, pattern `^[0-9]{6}$`) — used in most endpoints
- `secret_codes` (string) — used in `POST /deliveries`
- `edit_code` (integer) — used in `PATCH /drops/{dropId}/minting-config`

These all refer to the same concept. Note the type difference: `edit_code` is an integer, while `secret_code` is a string.

### Two Different Status Tracking Systems
After minting, there are two status tracking systems with different value sets:

1. **`tx_status`** (on Claim objects from `GET /actions/claim-qr`): documented values are `waiting_tx` | `pending` | `passed` | `failed` | `bumped`, but live responses may also return an empty string
2. **`status`** (from `GET /transaction-requests/{id}`): `IN_PROCESS` | `IN_PROCESS_WORKER` | `FINISH` | `FINISH_WITH_ERROR`

Use `transaction-requests` to poll for mint completion (status = `FINISH`).

### `chain` vs `layer`
Token endpoints use two different fields for the same concept:
- `chain`: `"homestead"` (Ethereum), `"xdai"` (Gnosis), `"base"`, `"matic"` (Polygon), `"arbitrum-one"`, `"celo"`, `"chiliz"`, `"mantle"`, `"unichain"` (on `GET /actions/scan/{address}` responses)
- `layer`: `"Layer1"` (Ethereum) or `"Layer2"` (all other chains) (on `GET /token/{tokenId}` responses)

`GET /actions/scan/{address}/{eventId}` is a smaller lookup response and does not include `chain`, `created`, or `migrated`.

### `timezone` Field Means Different Things
- On Event objects: IANA timezone string (e.g. `"America/New_York"`)
- On Secret Requests: numeric UTC offset (`-12` to `12`)

### Date Formats
The API accepts dates as `YYYY-MM-DD` or `MM-DD-YYYY` for input. However, responses return dates in `DD-Mon-YYYY` format (e.g. `"07-Dec-2021"`). The `/delivery-addresses/{id}/address/{address}` endpoint is an exception, returning ISO 8601 timestamps (e.g. `"2026-02-16T06:00:00.000Z"`).

### Plural Names That Accept Singular Values
- `event_ids` in deliveries is a **string** containing a single numeric ID (not an array)
- `secret_codes` in deliveries is a **string** containing a single six-digit code (not an array)

### Event Object Varies by Endpoint
The Event object returned by token/scan endpoints (`tokens.md`) is a subset — it omits `animation_url`, `from_admin`, `virtual_event`, `event_template_id`, `private_event`, and `drop_image`. On `GET /actions/scan/{address}`, it also adds `supply`. The canonical full Event object is defined in [events.md](events.md).

The full Event object itself also varies: `animation_url` is only present on `/paginated-events` (not on `/events/id` or `/events/{fancyId}`), while `created_date` is only present on `/events/id` and `/events/{fancyId}` (not on `/paginated-events`). `drop_image` may be absent on older events.

### Endpoint Path Prefixes Are Inconsistent
The API mixes singular and plural path prefixes:
- `/events/id/{id}`, `/events/{fancyId}`, `POST /events` (plural)
- `/event/validate`, `/event/{id}/qr-codes`, `/event/{id}/poaps` (singular)

### Pagination
Paginated endpoints use different response shapes:
| Endpoint | Array field | Max limit |
|----------|------------|-----------|
| `GET /deliveries` | `deliveries` | 1000 |
| `GET /delivery-addresses/{id}` | `items` | 1000 |
| `GET /paginated-events` | `items` | 1000 |
| `GET /event/{id}/poaps` | `tokens` | 300 |

All use `limit`, `offset`, and `total` for pagination control.

### Secret Word Cannot Be Claimed via API
The `"word"` type secret can only be claimed through the POAP Mobile app. Only `"website"` type secrets can be claimed programmatically via `POST /website/claim`.

### Deliveries Cannot Be Updated
You cannot add addresses to a delivery after creation. Include all addresses in the initial `POST /deliveries` request.

### Events Can Only Be Edited Within 30 Days
`PUT /events/{fancyId}` will return `EDITION_EXPIRED` if the event start date is more than 30 days ago.

### Minting Network Cannot Be Set to Mainnet via API
`PATCH /drops/{dropId}/minting-config` supports all chains except Ethereum Mainnet (chain ID 1).
