# PayPal (`paypal`)

Accept payments via PayPal Checkout.

## Overview

| | |
|---|---|
| Addon ID | `paypal` |
| Category | payment |
| Version | 1.0.0 |
| Category guide | [../README.md](../README.md) |

Only **one** payment addon can be active at a time.

## Enable and configure

1. Install this package under `app/addons/payments/paypal/`
2. Open **Admin → Payments → PayPal** at `/admin/payments/paypal`
3. Enter credentials and enable **Enable this payment processor**

## Configuration schema

| Field | Type | Description |
|-------|------|-------------|
| `client_id` | secret | PayPal REST client ID |
| `client_secret` | secret | PayPal REST client secret |
| `webhook_id` | secret | PayPal webhook ID for signature verification |
| `environment` | string | `sandbox` or `production` |
| `return_url` | string | Redirect after approval |
| `cancel_url` | string | Redirect when cancelled |

Secrets are stored in `addon_configs`, not in `.env`.

## Routes

### Public API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/payments/paypal/webhook` | PSP webhook endpoint |

Checkout uses the generic order endpoint: `POST /api/v1/orders/{order_id}/checkout`.

### Admin

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/payments/paypal` | Config form |
| POST | `/admin/payments/paypal/save` | Save config |

## Core integration

- **Storefront checkout:** `POST /api/v1/orders/{order_id}/checkout` → `PaymentAddon.create_payment()` → PayPal approve URL
- **Webhook:** `POST /api/v1/payments/paypal/webhook` → verify signature → `parse_webhook()` → core `process_payment_webhook()`
- **Flow:** create order (`intent: CAPTURE`) → buyer approves → `CHECKOUT.ORDER.APPROVED` triggers server capture → `PAYMENT.CAPTURE.COMPLETED` marks order paid
- **Amounts:** smallest currency unit (cents)

## Provider setup

Register webhook URL (replace `{PUBLIC_APP_URL}` with your public base URL):

```
{PUBLIC_APP_URL}/api/v1/payments/paypal/webhook
```

### Required webhook events

| Event | Role |
|-------|------|
| `CHECKOUT.ORDER.APPROVED` | Trigger server-side capture (does **not** mark paid) |
| `PAYMENT.CAPTURE.COMPLETED` | Mark order paid |

### Also register

| Event | Role |
|-------|------|
| `PAYMENT.CAPTURE.DENIED` | Capture failed — do not fulfill |
| `PAYMENT.CAPTURE.PENDING` | Wait — do not fulfill yet |
| `CHECKOUT.PAYMENT-APPROVAL.REVERSED` | Approval reversed before capture |

1. Create an app in the [PayPal Developer Dashboard](https://developer.paypal.com/).
2. Register the webhook URL and events above; paste the Webhook ID into admin config.
3. Use sandbox credentials for testing; switch `environment` to `production` for live.

## Package layout

```
paypal/
├── README.md
├── addon.py
├── routes.py
└── templates/
```

## See also

- [Payment addon development](../README.md)
- [Oshkelosh addon guide](../../README.md)
