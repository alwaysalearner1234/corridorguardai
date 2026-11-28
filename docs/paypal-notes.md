# PayPal notes (Task 8)

What was checked in PayPal's own documentation, and what the code does with it.
Everything below was read from the pages in the "Sources" section at the bottom.
No secret, no real account and no real money is involved: the demo uses the
**PayPal sandbox** only.

## Sandbox only

| Question | Answer | Where it is used |
|---|---|---|
| Which host? | `https://api-m.sandbox.paypal.com` | `SANDBOX_BASE_URL` in `backend/app/ingest/paypal_client.py` |
| Which hosts count as the sandbox? | `api-m.sandbox.paypal.com` and `api.sandbox.paypal.com` | `SANDBOX_HOSTS`, checked by `require_sandbox_url()` |
| What happens with another host? | The app refuses to start: `PayPalConfigError` | `main.py` makes the client first, before the graph is opened |

`require_sandbox_url()` compares the **host** of `PAYPAL_BASE_URL` with the
sandbox list, so `https://api-m.sandbox.paypal.com.example.org` is refused too.

## Access token

PayPal's own example (`/api/get-started`) is a form-encoded POST to the token
endpoint with HTTP Basic authentication:

| Part | Value |
|---|---|
| Method and URL | `POST https://api-m.sandbox.paypal.com/v1/oauth2/token` |
| Authorization | HTTP Basic: the user is the client id, the password is the client secret |
| Body | `grant_type=client_credentials`, form encoded |
| Answer | `access_token`, `token_type`, `expires_in` |

`PayPalClient.get_access_token()` does exactly this. The client id and the
client secret come from `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` only.
They are sent in the `Authorization` header and are never logged, never put in
an error message and never stored.

## Creating an order

`POST /v2/checkout/orders` with `intent` and `purchase_units`.

PayPal's words for the field we need, from the Orders v2 schema:

> `custom_id` - "The API caller-provided external ID. Used to reconcile API
> caller-initiated transactions with PayPal transactions. Appears in transaction
> and settlement reports."

So `PayPalClient.create_order(amount, currency, end_to_end_id)` puts the
end-to-end id in `purchase_units[0].custom_id` and the money in
`purchase_units[0].amount` (`currency_code` and `value`). The request also
carries PayPal's `PayPal-Request-Id` header with the same end-to-end id, so a
retry never creates a second order.

## Verifying a webhook

PayPal's guide says there are two ways to check a delivery and names the second
one:

> "Post the message, stored webhook ID, and header information back to PayPal's
> verify signature endpoint for it to do this check for you."

The endpoint is `POST /v1/notifications/verify-webhook-signature`. The request
body needs all seven fields below, and the answer has one:

| Field | Where it comes from |
|---|---|
| `auth_algo` | the `PAYPAL-AUTH-ALGO` header |
| `cert_url` | the `PAYPAL-CERT-URL` header |
| `transmission_id` | the `PAYPAL-TRANSMISSION-ID` header |
| `transmission_sig` | the `PAYPAL-TRANSMISSION-SIG` header |
| `transmission_time` | the `PAYPAL-TRANSMISSION-TIME` header |
| `webhook_id` | `PAYPAL_WEBHOOK_ID`, the id from the Developer Portal |
| `webhook_event` | the event PayPal sent, as JSON |

The answer is `verification_status`, and PayPal allows exactly two values:
`SUCCESS` and `FAILURE`. Only `SUCCESS` is accepted.

## Event names and field names

The event names PayPal documents that fit this project:

| Event | When |
|---|---|
| `CHECKOUT.ORDER.APPROVED` | "A buyer approved a checkout order" (Orders v2) |
| `CHECKOUT.ORDER.COMPLETED` | "A checkout order is processed" (marketplaces and platforms only) |
| `PAYMENT.CAPTURE.COMPLETED` | "A payment capture completes" |
| `PAYMENT.ORDER.CREATED` | "A payment order is created" |

The fields of the event itself:

| Field | Meaning |
|---|---|
| `id` | the id of the webhook event notification - this is the `eventId` on the graph |
| `create_time` | when the notification was created - this is the `ts` on the graph |
| `event_type` | the event name - this is the `type` on the graph |
| `resource_type` | the kind of resource, for example `checkout-order` or `capture` |
| `resource` | the object the event is about |

Inside the resource:

| Field | Meaning | Where it is used |
|---|---|---|
| `custom_id` | our end-to-end id | the `customId` on the graph, the field Task 9 matches on |
| `amount.value` | the amount | the `amount` on the graph |
| `amount.currency_code` | the currency | the `currency` on the graph |
| `id` | the PayPal id of the order or the capture | the `rawRef` on the graph |

`custom_id` and `amount` sit in different places depending on the event. For a
checkout order they are in `resource.purchase_units[0]`, for a capture they are
directly on `resource`. `event_to_paypal_event()` in
`backend/app/ingest/paypal_webhook.py` looks in both places.

## Does the sandbox accept INR?

**No.** PayPal's currency list has no Indian rupee in it. The currencies the
REST API supports are:

`AUD`, `BRL`, `CAD`, `CNY`, `CZK`, `DKK`, `EUR`, `HKD`, `HUF`, `ILS`, `JPY`,
`MYR`, `MXN`, `TWD`, `NZD`, `NOK`, `PHP`, `PLN`, `GBP`, `SGD`, `SEK`, `CHF`,
`THB`, `USD`

`INR` is not on that list, so **the demo uses USD**. Every recorded sandbox
delivery in `backend/tests/fixtures/paypal/` is in USD, and so is every amount
in the seed data.

## The replay fallback

The fallback for a demo without live PayPal is
`backend/app/ingest/paypal_replay.py`. It reads the recorded deliveries in
`backend/tests/fixtures/paypal/*.json` and pushes them through
`store_paypal_event()`, the same function the live route uses.

The signature check is not skipped. `local_test_verifier()` stands in for
PayPal's own check: it accepts a delivery only when it is one of the recorded
deliveries and it carries the transmission headers PayPal sent with it. It
**only works when `DEMO_MODE=local`**, because outside local demo mode a local
check is not an honest check.

The recorded `paypal-transmission-sig` values are **not** real PayPal
signatures. They are placeholders in a recorded delivery. Only PayPal's verify
endpoint can confirm a live delivery.

## Sources

| Page | What was taken from it |
|---|---|
| <https://developer.paypal.com/api/get-started> | the access token call, the sandbox URL, the shape of the answer |
| <https://developer.paypal.com/api/webhooks/overview> | the two ways to verify a delivery, and that the verify endpoint is the honest one |
| <https://developer.paypal.com/api/webhooks/v1> | the verify-webhook-signature operation and its schema |
| <https://developer.paypal.com/api/webhooks/v1/schema.json> | the seven request fields, the `verification_status` answer, the event schema |
| <https://developer.paypal.com/api/rest/webhooks/event-names> | the real event names |
| <https://developer.paypal.com/api/orders/v2/schema.json> | `purchase_units[].custom_id` and `amount` |
| <https://developer.paypal.com/reference/currency-codes> | the list of currencies, and the absence of INR |
