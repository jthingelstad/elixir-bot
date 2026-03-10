# Create a POAP

> Creating POAPs on behalf of users is not yet supported. See the phased approach in [guide-common-flows.md](guide-common-flows.md).

Quality guidelines: [Creating quality drops](https://poap.zendesk.com/hc/en-us/articles/9494120581773) | [Curation guidelines](https://curation.poap.xyz/guidelines)

## Step 1: Create a Drop

Use `POST /events` with Content-Type: `multipart/form-data` (not JSON — required because the `image` field is a binary upload):

```json
// Shown as JSON for readability; send as multipart/form-data fields
{
  "name": "Drop name",
  "description": "Drop description (follow curation guidelines)",
  "city": "City (or empty for virtual)",
  "country": "Country (or empty for virtual)",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "expiry_date": "YYYY-MM-DD (no minting after this date)",
  "event_url": "https://example.com",
  "virtual_event": true,
  "image": "<binary: 500x500, circle-cropped, under 4MB, .png/.gif/.webp>",
  "secret_code": "123456 (six-digit edit code, save this)",
  "event_template_id": 1,
  "email": "you@example.com (receives mint links)",
  "requested_codes": 10,
  "private_event": false
}
```

Set `requested_codes` to `0` if using a different distribution method.

## Step 2: Choose a Distribution Method

Distribution methods: **Mint Links**, **Website**, **Secret Word**, and **Delivery**. Learn more: [Distribution Methods 101](https://poap.zendesk.com/hc/en-us/articles/9721389158029)

After submission, the POAP Curation Body reviews the request. Once approved, distribution is activated.

### Mint Links

Use `POST /redeem-requests`:

```json
{
  "event_id": 12345,
  "requested_codes": 100,
  "secret_code": "123456",
  "redeem_type": "qr_code"
}
```

Once approved, mint links are emailed. Collectors click the link and enter their Ethereum address or email.

### Secret Word or Website

Use `POST /secret-requests`:

```json
{
  "event_id": 12345,
  "requested_codes": 100,
  "secret_code": "123456",
  "claim_name": "your-secret-phrase",
  "from": "2022-07-18T00:01:00.000",
  "to": "2022-07-20T00:01:00.000",
  "timezone": 0,
  "active": true,
  "secret_type": "website"
}
```

- **Website:** Collectors visit `https://poap.website/{claim_name}` during the active window.
- **Secret Word:** Collectors enter the word in the POAP Mobile app ([iOS](https://poap.xyz/ios), [Android](https://poap.xyz/android)). Not claimable via API.

### Delivery

Use `POST /deliveries`:

```json
{
  "slug": "your-delivery-slug",
  "card_title": "Card title",
  "card_text": "Card description",
  "page_title": "Page title",
  "page_text": "Page description",
  "event_ids": "12345",
  "secret_codes": "123456",
  "image": "https://example.com/card-image.png",
  "page_title_image": "https://example.com/page-image.png",
  "metadata_title": "Social preview title",
  "metadata_description": "Social preview description",
  "addresses": [
    { "address": "0x1234...", "events": [12345] }
  ]
}
```

> Add all addresses in the first request — you cannot add addresses retroactively.

Collectors claim at `https://poap.delivery/{slug}` or via `POST /actions/claim-delivery-v2`.

## Step 3: Mint POAPs

### Via Mint Links

Both endpoints are protected (require auth token).

1. `GET /actions/claim-qr?qr_hash={code}` — retrieve the `secret` for a mint link.
2. `POST /actions/claim-qr` with `address`, `qr_hash`, and `secret` — mint the POAP.

Each mint link is single-use.

### Via Website

Call `POST /website/claim` with `website` (the `claim_name`) and `address`. Only works during the active window. Secret Word cannot be minted via API.

### Via Delivery

Call `POST /actions/claim-delivery-v2` with the delivery `id` and collector `address`.
