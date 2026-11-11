# PayPal (`paypal`) — AGENTS

## Role

Accept payments via PayPal Checkout. Addon ID: `paypal`.

## File map

- `README.md`
- `__init__.py`
- `addon.py`
- `oshkelosh-addon.json`
- `routes.py`
- `templates/`
- `tests/`

## Package specifics

**Category ceiling:** Only one payment addon active (recommended); `parse_webhook` is parse-only — core owns idempotency/DB writes.
**Config fields:** `client_id` (secret), `client_secret` (secret), `webhook_id` (secret), `environment` (string), `return_url` (string), `cancel_url` (string)

## Invariants

- Implement the matching category ABC; do not patch host `app/services/*.py` with provider branches
- Credentials and enable flags live in `addon_configs` (admin UI), not host `.env`
- Use `app/addons/log.py` for structured logging
- Admin mutating forms need CSRF; use `render_addon_admin_page` correctly (do not pass `flash` via `**_common_ctx`)
- Webhook parse paths must not write the DB — core owns idempotency
- Nested `.git` (if present): this package is its own repo; the host gitignores this directory

## Prefer / Avoid

- Prefer: extend this package and the category ABC; keep provider API clients here
- Avoid: editing host `app/services/` for this provider; putting secrets in host `.env`

## See also

- [README.md](README.md) — package how-to
- [../README.md](../README.md) — category guide
- [../../AGENTS.md](../../AGENTS.md) — host addon boundary
- [../../README.md](../../README.md) — plugin development
- [../../../../AGENTS.md](../../../../AGENTS.md), [../../../../DESIGN.md](../../../../DESIGN.md)
