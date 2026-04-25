# Setup and Authentication

## Endpoint Types

1. **Open endpoints** — require only an API key (`x-api-key` header).
2. **Protected endpoints** — require an API key **and** a valid access token (`Authorization: Bearer` header).

Exception: `GET /metadata/{eventId}/{tokenId}` requires no authentication.

## Protected Endpoints

These endpoints require an access token:

- `POST /actions/claim-qr` — Mint a POAP with a mint link
- `GET /actions/claim-qr` — Get mint link status and details
- `POST /event/validate` — Validate a secret code for an event
- `POST /event/{id}/qr-codes` — Get mint-link codes for an event
- `POST /redeem-requests` — Request additional mint codes
- `GET /secret/{secret_word}` — Get secret word claim info

## Get API Access

Request API keys and auth credentials: https://documentation.poap.tech/docs/api-access-request

You will receive:
- **API key** — for all requests
- **Client ID** and **Client Secret** — for generating access tokens

> **Note:** If you get errors on first use, your API key may not be active yet. Wait **12 hours** and try again. Auth token credentials (Client ID/Secret) are sent separately and may take up to **2 business days** after approval.

## Calling Open Endpoints

Include `x-api-key` in all requests (HTTP headers are case-insensitive):

```bash
curl -H "x-api-key: $apikey" https://api.poap.tech/events/id/16947
```

## Generating an Access Token

Access tokens are valid for **24 hours** and are of **dynamic length** (do not assume a fixed token size). Regenerate as needed.

> **Rate limit:** Requesting more than **4 access tokens per hour** will result in a ban. Cache and reuse your token rather than generating a new one for every request.

```bash
curl --request POST \
     --url 'https://auth.accounts.poap.xyz/oauth/token' \
     --header 'Content-Type: application/json' \
     --data '{
       "audience": "https://api.poap.tech",
       "grant_type": "client_credentials",
       "client_id": "$clientid",
       "client_secret": "$clientsecret"
     }'
```

## Calling Protected Endpoints

Include both `x-api-key` and `Authorization: Bearer` headers:

```bash
curl --request GET \
     --url 'https://api.poap.tech/actions/claim-qr?qr_hash=1kozmm' \
     --header 'Accept: application/json' \
     --header 'Authorization: Bearer $accesstoken' \
     --header 'x-api-key: $apikey'
```

## Security

Store API keys, auth tokens, and access tokens securely (secrets manager or environment variables). Never expose them in application code.
