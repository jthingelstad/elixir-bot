# Clash Royale API – Challenges Endpoint Status

Base URL: `https://api.clashroyale.com/v1` Auth: Bearer token in `Authorization` header

Gameplay context: use [events.md](events.md), [game-modes.md](game-modes.md#event-and-challenge-variants), and
[wiki-api-crosswalk.md](wiki-api-crosswalk.md#events-challenges-and-temporary-modes) because challenge-like modes are
currently observable through `/events` even though `/challenges` is unavailable in live testing.

---

## Endpoint

### GET /challenges

Get all current and upcoming challenges.

**Query:** None

**Returns:** `ChallengeChainsList` — array of `ChallengeChain` objects

**Chain structure:**

- Each chain is of type `singleChallenge` or `challengeChain`
- Prize types: `none`, `cardStack`, `chest`, `cardStackRandom`, `resource`, `tradeToken`, `consumable`

**Current status:** This endpoint is not shown in the official Swagger UI as of the June 2026 documentation review. It
returned `{"reason":"notFound"}` in March 2026 live testing. Challenge information may be available through the
`/events` endpoint instead.

---

## Error Codes

| Code | Meaning                                 |
| ---- | --------------------------------------- |
| 400  | Bad parameters                          |
| 403  | Auth failure / insufficient token scope |
| 404  | Not found                               |
| 429  | Rate limit exceeded                     |
| 500  | Server error                            |
| 503  | Maintenance                             |

Observed error bodies are usually `{ reason, message? }`. In the March 2026 pass this endpoint returned only
`{ "reason": "notFound" }`. `type`/`detail` were not observed.

---

## Agent Notes

- This endpoint is not currently documented in the official Swagger UI
- This endpoint returned `notFound` in testing (March 2026) — may be unavailable or deprecated
- Challenge events still appear in `/events` (e.g. "Classic Challenge", "Grand Challenge", "Mega Draft Challenge") — use
  that endpoint to detect active challenges
- If the endpoint does return data, `singleChallenge` is a standalone challenge; `challengeChain` is a sequence that
  must be completed in order
- Prize type `cardStackRandom` indicates a random card reward
