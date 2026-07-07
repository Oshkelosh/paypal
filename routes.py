"""PayPal addon routes — thin delegates to shared payment route factory."""

from __future__ import annotations

from typing import Any

from app.addons.payments.shared_routes import build_payment_routers


def _parse_paypal_config_form(form: Any) -> tuple[dict[str, Any], bool]:
    return (
        {
            "client_id": form.get("client_id", ""),
            "client_secret": form.get("client_secret", ""),
            "webhook_id": form.get("webhook_id", ""),
            "environment": form.get("environment", "sandbox"),
            "return_url": form.get("return_url", ""),
            "cancel_url": form.get("cancel_url", ""),
        },
        form.get("is_enabled") == "on",
    )


admin_router, api_router, jinja_env = build_payment_routers(
    "paypal",
    template_name="paypal_config.html",
    page_title="PayPal Settings",
    secret_keys=("client_id", "client_secret", "webhook_id"),
    signature_header="paypal-transmission-id",
    parse_config_form=_parse_paypal_config_form,
)
