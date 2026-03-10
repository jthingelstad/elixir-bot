# POAP Utils API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

---

## Endpoints

### GET /health-check

Health check for the API. A 200 status indicates the API is up and running.

**Response** (200):

```json
{ "status": "healthy" }
```

---

### POST /drops/{dropId}/minting-config

Get the minting configuration for a specific drop.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `dropId` | string | Numeric drop/event ID |

**Response** (200): Object (schema not specified)

---

### PATCH /drops/{dropId}/minting-config

Update the blockchain used for minting a drop. Currently only one blockchain per drop is supported.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `dropId` | string | Numeric drop ID |

**Request Body** (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `edit_code` | integer | yes | Six-digit edit code set at drop creation (min: 1) |
| `supported_mint_blockchains` | number[]\|null | no | Array of blockchain network IDs (one per drop). Default: `[100]` (Gnosis) |

**Supported blockchain network IDs:**

| Chain ID | Network |
|----------|---------|
| `100` | Gnosis (default) |
| `137` | Polygon |
| `130` | Unichain |
| `5000` | Mantle |
| `8453` | Base |
| `33139` | Apechain |
| `42161` | Arbitrum |
| `42220` | Celo |
| `59144` | Linea |
| `88888` | Chiliz |

**Response** (200): Object (schema not specified)
