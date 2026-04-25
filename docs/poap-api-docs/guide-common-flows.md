# Common API Flows

## 1. Mint a POAP Directly to a Wallet

**Prerequisites:** API key, auth token, a created event, collector's Ethereum address/ENS/email.

**Flow:**

1. Call `POST /event/{id}/qr-codes` with the event's `secret_code` to get mint-link `qr_hash` codes.
2. For each mint, call `GET /actions/claim-qr?qr_hash={code}` to retrieve the `secret`.
3. Call `POST /actions/claim-qr` with `address`, `qr_hash`, and `secret` to mint the POAP.

Need more codes? Use `POST /redeem-requests`.

---

## 2. Display a POAP Collection

**Prerequisites:** API key, collector's Ethereum address/ENS/email.

**Flow:**

1. Call `GET /actions/scan/{address}` to fetch all POAPs held by an address.
2. Use the `image_url` from each result to display artwork. Append `?size=small` (or `xsmall`, `medium`, `large`, `xlarge`) for compressed images.

---

## 3. Token Gating With POAPs

**Prerequisites:** API key, a POAP event ID.

**Options:**

- **Option 1 (fastest):** `GET /actions/scan/{address}/{eventId}` — returns `{ event, tokenId, owner }` (200) if the address holds the POAP, or 404 if not.
- **Option 2:** `GET /actions/scan/{address}` — get all POAPs for an address, then check for the target event ID client-side.
- **Option 3:** `GET /event/{id}/poaps` — get all addresses holding a POAP for an event, then check if the user's address is in the list.

---

## 4. Create an Event, Submit a Request, and Check Status

**Prerequisites:** API key, auth token, event details (see `POST /events` reference).

> **Testing etiquette:** Include "test" in the title, add context in the description, don't reuse production artwork, request no more than 10 mint links, use a real email, label sequential tests (e.g. "test 1 of 3").

**Flow:**

1. **Create the event:** `POST /events` (multipart/form-data).
   - Define a six-digit `secret_code` and store it.
   - Set `requested_codes` to the number of mint links needed (or `0` if using another distribution method).
   - On success, save the returned `id` for subsequent calls.

2. **Submit a distribution request** (skip if `requested_codes > 0` in step 1):
   - Use `POST /redeem-requests` for mint links, or `POST /secret-requests` for Secret Word/Website.
   - One pending request per distribution method is allowed at a time.

3. **Wait for Curation Body review** (up to 24 hours):
   - Poll `GET /redeem-requests/active/count?event_id={id}&redeem_type=qr_code` — an empty array `[]` means no pending requests. Note: `redeem_type` is required.
   - The Curation Body may email `curation@poap.io` requesting changes.

4. **Retrieve mint links:** `POST /event/{id}/qr-codes` with `secret_code`.
   - Each code shows its claim status.

5. **Request top-ups:** `POST /redeem-requests` with the event's `event_id`, `requested_codes`, and `secret_code`. Goes back through Curation review.

> **Creating POAPs on behalf of users:**
> Phase 1 (supported): Allow users to bring an already-created POAP and edit code.
> Phase 2 (not yet supported): Requires successful Phase 1 completion, curation compliance, and Integration team approval.
