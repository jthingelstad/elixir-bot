# POAP API Docs

This repo is a working Markdown reference for the POAP API.

It is not an SDK, app, or test harness. The primary contents are endpoint references, workflow guides, and notes about quirks discovered through live verification.

## What Is Here

- `index.md` is the best starting point.
- `setup-and-authentication.md` explains API keys and bearer tokens.
- `events.md`, `tokens.md`, `claims.md`, `delivery.md`, `secret-requests.md`, `redeem-requests.md`, `queues.md`, `utils.md`, and `qr-code-generate.md` document endpoint groups.
- `guide-common-flows.md` and `guide-create-poap.md` describe higher-level usage patterns.
- `errors.md` and `smart-contract-reference.md` cover supporting reference material.

## What This Repo Is For

Use this repo when you need to:

- understand POAP API request and response shapes
- find auth requirements for a specific endpoint
- follow common POAP integration flows
- verify whether the docs match current live API behavior

## Guidance For Agents

Agents working in this repo should treat it as a documentation source of truth that still needs verification against the live API when accuracy matters.

Rules:

- Start with `index.md`, then read only the files relevant to the task.
- Prefer read-only verification against the live API when checking factual accuracy.
- Do not call endpoints that create, update, claim, mint, or otherwise write data unless the user explicitly asks for that and understands the risk.
- If event IDs, edit codes, QR hashes, or claim names are provided, treat them as sensitive operational data and avoid echoing secrets unnecessarily.
- When live behavior differs from the docs, update the Markdown to describe observed behavior precisely and conservatively.
- Do not assume response fields are always present just because one sample included them.
- Keep examples and notes practical for future agents and integrators.

## Safe Verification Approach

Preferred order for doc validation:

1. Read the relevant Markdown file.
2. Classify endpoints as read-only or write-capable.
3. Test only read-safe endpoints by default.
4. Record mismatches between documented and observed behavior.
5. Patch the docs, keeping claims narrow and evidence-based.

Examples of read-safe checks:

- `GET /events/id/{id}`
- `GET /events/{fancyId}`
- `GET /paginated-events`
- `GET /actions/scan/{address}`
- `GET /actions/scan/{address}/{eventId}`
- `GET /event/{id}/poaps`
- `GET /token/{tokenId}`
- `GET /token/{tokenId}/image`
- `GET /metadata/{eventId}/{tokenId}`
- `GET /transaction-requests/{id}`
- `GET /redeem-requests/active/count`
- protected read endpoints such as `GET /actions/claim-qr` or `GET /secret/{secret_word}` when used only for inspection

Examples of endpoints to avoid unless explicitly requested:

- `POST /actions/claim-qr`
- `POST /events`
- `PUT /events/{fancyId}`
- `POST /deliveries`
- `POST /secret-requests`
- `PUT /secret-requests`
- `POST /website/claim`
- `POST /redeem-requests`
- `PATCH /drops/{dropId}/minting-config`

## Editing Expectations

- Keep the docs in plain Markdown.
- Favor accuracy over completeness-by-assumption.
- Document quirks and optional fields when they are observed live.
- Avoid leaking tokens, secrets, edit codes, or claim secrets into committed files.

