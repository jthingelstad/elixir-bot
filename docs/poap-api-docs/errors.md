# POAP API Errors

POAP uses standard HTTP response codes. Handle all possible API exceptions gracefully.

## 400 Bad Request

| Code | Description |
|------|-------------|
| `SECRET_CODE_MISSING` | The secret code (edit code) is missing from the request |
| `LIMIT_EXCEEDED` | All available POAPs have been minted. No additional mints available for the Secret Word or Website delivery method |
| `INVALID_SECRET` | The Secret Word provided is not valid. Contact the issuer |
| `INVALID_SECRET_CODE` | The secret code (edit code) provided is invalid |
| `SNAPSHOT_BAD_REQUEST` | Bad request to Snapshot. Check the parameters against the documentation |
| `EDITION_EXPIRED` | The editing window has expired. Events can only be edited within 30 days of the start date |
| `EVENT_EXPIRED` | The event timeframe has expired |
| `EVENT_EDIT_BAD_REQUEST` | Malformed request when editing an event |

## 401 Unauthorized

| Code | Description |
|------|-------------|
| `INVALID_CREDENTIALS` | Credentials are incorrect or invalid. Request credentials at https://documentation.poap.tech/docs/api-access |
| `UNAUTHORIZED` | Your auth token does not have the required permissions for this resource |
| `ERROR` | Missing headers or invalid/incorrect tokens. Verify headers and token validity |

## 403 Forbidden

| Code | Description |
|------|-------------|
| `FORBIDDEN` | Auth token does not have authorization to access this resource |

## 404 Not Found

| Code | Description |
|------|-------------|
| `NOT_FOUND` | The requested resource does not exist or is invalid |
| `EVENT_NOT_FOUND` | The event ID does not exist or is invalid |
| `SNAPSHOT_NOT_FOUND` | No Snapshot response found. Check the parameters |
| `THEGRAPH_INTERNAL_SERVER_ERROR` | Internal server error or mishandled response from The Graph |

## 500 Internal Server Error

| Code | Description |
|------|-------------|
| `SNAPSHOT_INTERNAL_SERVER_ERROR` | Internal error or mishandled response from Snapshot |
| `ERROR` | The API is down. Report to tech@poap.io |
