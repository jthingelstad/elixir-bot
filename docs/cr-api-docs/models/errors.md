# Error And Utility Models

Error behavior verified against live API responses (March-April 2026).

## ClientError

Observed error bodies are usually:

```json
{ "reason": "badRequest", "message": "..." }
```

or:

```json
{ "reason": "notFound" }
```

`reason` is present in observed responses. `message` is present on many `400` responses but often absent on `404` and
`500` responses. `type` and `detail` were not observed.

Known `reason` values:

- `accessDenied`
- `notFound`
- `gone`
- `badRequest`
- `unknownException`

Common status codes:

| Code | Meaning                                                                         |
| ---: | ------------------------------------------------------------------------------- |
|  400 | Bad parameters                                                                  |
|  403 | Auth failure, insufficient token scope, IP mismatch, or sometimes rate limiting |
|  404 | Resource not found                                                              |
|  410 | Endpoint permanently removed                                                    |
|  429 | Rate limit exceeded                                                             |
|  500 | Server error                                                                    |
|  503 | Maintenance                                                                     |

## Pagination Wrapper

Paginated endpoints return:

```json
{
  "items": [],
  "paging": {
    "cursors": {
      "after": "eyJwb3MiOjV9"
    }
  }
}
```

Notes:

- `after` and `before` are mutually exclusive.
- Empty `cursors: {}` means no more pages.
- Cursors are base64-encoded JSON, such as `{"pos":5}`.
- Do not assume every response with `items` is paginated. Presence of `paging` is the reliable signal.

## Datetime

All observed datetime strings use:

```text
YYYYMMDDTHHmmss.sssZ
```

Example:

```text
20260309T135844.000Z
```

## Utility Models

The public Swagger surface contains several generic or inaccessible utility models:

| Model                                                                              | Notes                         |
| ---------------------------------------------------------------------------------- | ----------------------------- |
| `Version`                                                                          | API version metadata          |
| `Fingerprint`                                                                      | Device/session fingerprint    |
| `JsonNode`                                                                         | Generic untyped JSON node     |
| `Match` / `RegisterMatchRequest` / `RegisterMatchResponse` / `CancelMatchResponse` | No public endpoint observed   |
| `VerifyTokenRequest` / `VerifyTokenResponse`                                       | Restricted token verification |

Treat these as non-primary for public Clash Royale API integrations unless an official endpoint exposes them.
