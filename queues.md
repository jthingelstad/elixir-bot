# POAP Queues API Reference

Base URL: `https://api.poap.tech`

## Authentication

All endpoints require an API key via header:

```
x-api-key: <your-api-key>
```

---

## Endpoints

### GET /transaction-requests/{id}

Check the status of a POAP minting transaction. Use the `transaction_request_id` returned from a claim endpoint to poll this.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Transaction request ID |

**Response** (200):

| Field | Type | Description |
|-------|------|-------------|
| `operation` | string | The operation type (e.g. `"mintDeliveryTokenV2"`) |
| `status` | string | Minting status: `IN_PROCESS`, `IN_PROCESS_WORKER`, `FINISH`, or `FINISH_WITH_ERROR` |
| `result` | object | Contains `tx_hash` (string) — the minting transaction hash. **Note:** This field is often absent from responses, even when `status` is `FINISH`. Do not rely on its presence |
