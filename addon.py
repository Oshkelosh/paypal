"""
PayPal payment integration.

Supports checkout orders, captures, refunds, and webhooks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr

from app.addons.payments.base import PaymentAddon
from app.addons.payments.helpers import effective_redirect_url, extract_order_id, mock_checkout
from schemas.payment import PaymentWebhookOutcome
from app.addons.log import info, warning
from app.addons.config_serialization import dump_addon_config

PayPalEnvironment = Literal["sandbox", "production"]

_API_BASES: dict[PayPalEnvironment, str] = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "production": "https://api-m.paypal.com",
}


class PayPalConfig(BaseModel):
    client_id: SecretStr = Field(default=..., description="PayPal REST client ID")
    client_secret: SecretStr = Field(default=..., description="PayPal REST client secret")
    webhook_id: SecretStr = Field(default=..., description="PayPal webhook ID for verification")
    environment: PayPalEnvironment = Field(default="sandbox")
    return_url: str = Field(
        default="",
        description="Optional override for return redirect (leave blank to use Site URL)",
    )
    cancel_url: str = Field(
        default="",
        description="Optional override for cancel redirect (leave blank to use Site URL)",
    )

    @classmethod
    def config_model(cls):
        return cls


class PayPalAddon(PaymentAddon):
    addon_id: str = "paypal"
    addon_name: str = "PayPal"
    addon_description: str = "Accept payments via PayPal Checkout."
    addon_category: str = "payment"
    version: str = "1.0.0"
    is_enabled: bool = False

    _config: Dict[str, Any] | None = None
    _client_id: str | None = None
    _client_secret: str | None = None
    _webhook_id: str | None = None
    _environment: PayPalEnvironment = "sandbox"
    _return_url: str = ""
    _cancel_url: str = ""
    _api_base: str = _API_BASES["sandbox"]
    _access_token: str | None = None

    @classmethod
    def config_schema(cls):
        return PayPalConfig

    async def initialize(self, config: dict) -> None:
        validated = self.config_schema()(**config)
        self._config = dump_addon_config(validated)
        self._client_id = validated.client_id.get_secret_value()
        self._client_secret = validated.client_secret.get_secret_value()
        self._webhook_id = validated.webhook_id.get_secret_value()
        self._environment = validated.environment
        self._return_url = validated.return_url
        self._cancel_url = validated.cancel_url
        self._api_base = _API_BASES[self._environment]
        self._access_token = None
        self.is_enabled = True
        info("PayPal", "Initialized (environment={})", self._environment)

    async def validate_config(self, config: dict) -> None:
        from app.core.exceptions import ValidationError

        validated = self.config_schema()(**config)
        client_id = validated.client_id.get_secret_value()
        client_secret = validated.client_secret.get_secret_value()
        if not client_id or not client_secret:
            return
        api_base = _API_BASES[validated.environment]
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{api_base}/v1/oauth2/token",
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
        if resp.status_code == 401:
            raise ValidationError(message="Invalid client credentials — check your credentials")
        if resp.status_code == 403:
            raise ValidationError(
                message="Credentials are valid but missing required permissions: payments"
            )
        if resp.status_code >= 400:
            raise ValidationError(message="PayPal rejected the client credentials")

    async def shutdown(self) -> None:
        self._client_id = None
        self._client_secret = None
        self._webhook_id = None
        self._access_token = None
        self.is_enabled = False

    async def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self._client_id or not self._client_secret:
            raise RuntimeError("PayPal credentials not configured")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._api_base}/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            self._access_token = resp.json()["access_token"]
            return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(
                method,
                f"{self._api_base}{path}",
                headers=headers,
                json=json_body,
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {}

    async def create_payment(
        self,
        amount: int,
        currency: str,
        order_id: str,
        customer_email: str,
        *,
        return_url: str | None = None,
        cancel_url: str | None = None,
    ) -> Dict[str, Any]:
        if not self._client_id:
            return mock_checkout("paypal", order_id, amount, currency)

        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": order_id,
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": f"{amount / 100:.2f}",
                    },
                    "custom_id": order_id,
                }
            ],
            "application_context": {
                "return_url": effective_redirect_url(
                    self._return_url, fallback=return_url or ""
                ),
                "cancel_url": effective_redirect_url(
                    self._cancel_url, fallback=cancel_url or ""
                ),
                "user_action": "PAY_NOW",
            },
        }
        if customer_email:
            body["payer"] = {"email_address": customer_email}

        try:
            data = await self._request("POST", "/v2/checkout/orders", json_body=body)
            approve_link = next(
                (link["href"] for link in data.get("links", []) if link.get("rel") == "approve"),
                "",
            )
            return {
                "success": True,
                "payment_id": data.get("id", ""),
                "session_id": data.get("id", ""),
                "url": approve_link,
                "order_id": order_id,
            }
        except Exception as exc:
            warning("PayPal", "create_payment error: {}", exc)
            return mock_checkout("paypal", order_id, amount, currency)

    async def confirm_payment(self, payment_id: str) -> Dict[str, Any]:
        if not self._client_id:
            return {"success": False, "error": "PayPal credentials not configured"}

        try:
            data = await self._request("POST", f"/v2/checkout/orders/{payment_id}/capture")
            status = data.get("status", "unknown")
            capture = (
                data.get("purchase_units", [{}])[0]
                .get("payments", {})
                .get("captures", [{}])[0]
            )
            return {
                "success": status == "COMPLETED",
                "payment_id": payment_id,
                "status": status.lower(),
                "amount": int(float(capture.get("amount", {}).get("value", 0)) * 100),
            }
        except Exception as exc:
            warning("PayPal", "confirm_payment({}) error: {}", payment_id, exc)
            return {"success": False, "error": str(exc)}

    async def refund_payment(self, payment_id: str, amount: int) -> Dict[str, Any]:
        if not self._client_id:
            return {"success": False, "error": "PayPal credentials not configured"}

        body = {
            "amount": {"value": f"{amount / 100:.2f}", "currency_code": "USD"},
        }
        try:
            data = await self._request(
                "POST",
                f"/v2/payments/captures/{payment_id}/refund",
                json_body=body,
            )
            return {
                "success": True,
                "refund_id": data.get("id", ""),
                "amount": amount,
                "status": data.get("status", "COMPLETED").lower(),
            }
        except Exception as exc:
            warning("PayPal", "refund_payment({}) error: {}", payment_id, exc)
            return {"success": False, "error": str(exc)}

    async def get_payment_status(self, payment_id: str) -> Dict[str, Any]:
        if not self._client_id:
            return {"payment_id": payment_id, "status": "error", "detail": "Not configured"}

        try:
            data = await self._request("GET", f"/v2/checkout/orders/{payment_id}")
            return {
                "payment_id": payment_id,
                "status": data.get("status", "unknown").lower(),
                "amount": int(
                    float(
                        data.get("purchase_units", [{}])[0]
                        .get("amount", {})
                        .get("value", 0)
                    )
                    * 100
                ),
                "currency": (
                    data.get("purchase_units", [{}])[0]
                    .get("amount", {})
                    .get("currency_code", "usd")
                    .lower()
                ),
            }
        except Exception as exc:
            warning("PayPal", "get_payment_status({}) error: {}", payment_id, exc)
            return {"payment_id": payment_id, "status": "error", "detail": str(exc)}

    def webhook_signature_header(self) -> str:
        return "paypal-transmission-id"

    async def parse_webhook(
        self, payload: Dict[str, Any], signature: str
    ) -> PaymentWebhookOutcome:
        try:
            event_type = payload.get("event_type", "")
            event_data = payload.get("resource", payload)
            event_id = str(payload.get("id", ""))
            info("PayPal", "Webhook received: {}", event_type)

            if event_type in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
                metadata = dict(event_data.get("metadata") or {})
                if event_data.get("custom_id"):
                    metadata.setdefault("order_id", event_data["custom_id"])
                order_id = extract_order_id(metadata)
                payment_id = event_data.get("id") or event_data.get("supplementary_data", {}).get(
                    "related_ids", {}
                ).get("order_id")
                return PaymentWebhookOutcome(
                    handled=True,
                    event_id=event_id,
                    event_type=event_type,
                    mark_paid=order_id is not None,
                    order_id=order_id,
                    payment_id=str(payment_id) if payment_id else None,
                )

            return PaymentWebhookOutcome(
                handled=True,
                event_id=event_id,
                event_type=event_type,
            )
        except Exception as exc:
            warning("PayPal", "parse_webhook error: {}", exc)
            return PaymentWebhookOutcome(handled=False, error=str(exc))

    def get_routers(self) -> List[APIRouter]:
        from app.addons.payments.paypal.routes import api_router

        return [api_router]

    def get_admin_routes(self) -> List[APIRouter]:
        from app.addons.payments.paypal.routes import admin_router

        return [admin_router]

    def get_admin_templates(self) -> str:
        from pathlib import Path

        return str(Path(__file__).resolve().parent / "templates")

    def get_admin_static(self) -> str:
        from pathlib import Path

        return str(Path(__file__).resolve().parent / "static")
