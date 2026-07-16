"""
PayPal payment integration.

Supports checkout orders, captures, refunds, and webhooks.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Literal, Mapping

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr

from app.addons.config_serialization import dump_addon_config
from app.addons.log import info, warning
from app.addons.payments.base import PaymentAddon
from app.addons.payments.helpers import effective_redirect_url, extract_order_id, mock_checkout
from schemas.payment import PaymentWebhookOutcome

PayPalEnvironment = Literal["sandbox", "production"]

_API_BASES: dict[PayPalEnvironment, str] = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "production": "https://api-m.paypal.com",
}

_TOKEN_REFRESH_SKEW_SECONDS = 60


def _header_get(headers: Mapping[str, str], name: str) -> str:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def _resolve_internal_order_id(resource: dict[str, Any]) -> int | None:
    """Resolve core order id from a PayPal webhook resource."""
    metadata = dict(resource.get("metadata") or {})
    if resource.get("custom_id"):
        metadata.setdefault("order_id", resource["custom_id"])
    units = resource.get("purchase_units") or []
    if units and isinstance(units[0], dict) and units[0].get("custom_id"):
        metadata.setdefault("order_id", units[0]["custom_id"])
    return extract_order_id(metadata)


def _paypal_order_id(resource: dict[str, Any]) -> str | None:
    """PayPal Checkout Order id (not the core order id)."""
    related = (
        resource.get("supplementary_data", {})
        .get("related_ids", {})
        .get("order_id")
    )
    if related:
        return str(related)
    if resource.get("id"):
        return str(resource["id"])
    return None


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
    _token_expires_at: float = 0.0

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
        self._token_expires_at = 0.0
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
        self._token_expires_at = 0.0
        self.is_enabled = False

    async def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
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
            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 32400))
            self._token_expires_at = now + max(expires_in - _TOKEN_REFRESH_SKEW_SECONDS, 60)
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
            return {"success": False, "error": str(exc), "order_id": order_id}

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
            capture_id = capture.get("id") or payment_id
            return {
                "success": status == "COMPLETED",
                "payment_id": payment_id,
                "capture_id": capture_id,
                "status": status.lower(),
                "amount": int(float(capture.get("amount", {}).get("value", 0)) * 100),
            }
        except Exception as exc:
            warning("PayPal", "confirm_payment({}) error: {}", payment_id, exc)
            return {"success": False, "error": str(exc)}

    async def refund_payment(self, payment_id: str, amount: int) -> Dict[str, Any]:
        if not self._client_id:
            return {"success": False, "error": "PayPal credentials not configured"}

        try:
            capture = await self._request("GET", f"/v2/payments/captures/{payment_id}")
            currency = (
                capture.get("amount", {}).get("currency_code") or "USD"
            ).upper()
            body = {
                "amount": {"value": f"{amount / 100:.2f}", "currency_code": currency},
            }
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

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> bool:
        """Verify a PayPal webhook via the notifications verify API."""
        if not self._webhook_id or not self._client_id:
            warning("PayPal", "verify_webhook skipped: webhook_id or credentials missing")
            return False

        transmission_id = _header_get(headers, "paypal-transmission-id")
        transmission_time = _header_get(headers, "paypal-transmission-time")
        transmission_sig = _header_get(headers, "paypal-transmission-sig")
        cert_url = _header_get(headers, "paypal-cert-url")
        auth_algo = _header_get(headers, "paypal-auth-algo")
        if not all(
            (transmission_id, transmission_time, transmission_sig, cert_url, auth_algo)
        ):
            warning("PayPal", "verify_webhook missing required PayPal headers")
            return False

        try:
            webhook_event = json.loads(body.decode("utf-8"))
            result = await self._request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                json_body={
                    "transmission_id": transmission_id,
                    "transmission_time": transmission_time,
                    "cert_url": cert_url,
                    "auth_algo": auth_algo,
                    "transmission_sig": transmission_sig,
                    "webhook_id": self._webhook_id,
                    "webhook_event": webhook_event,
                },
            )
            status = result.get("verification_status", "")
            ok = status == "SUCCESS"
            if not ok:
                warning("PayPal", "verify_webhook status={}", status)
            return ok
        except Exception as exc:
            warning("PayPal", "verify_webhook error: {}", exc)
            return False

    async def parse_webhook(
        self, payload: Dict[str, Any], signature: str
    ) -> PaymentWebhookOutcome:
        try:
            event_type = payload.get("event_type", "")
            event_data = payload.get("resource", payload)
            if not isinstance(event_data, dict):
                event_data = {}
            event_id = str(payload.get("id", ""))
            info("PayPal", "Webhook received: {}", event_type)

            if event_type == "CHECKOUT.ORDER.APPROVED":
                paypal_order_id = _paypal_order_id(event_data)
                if not paypal_order_id:
                    warning("PayPal", "APPROVED webhook missing PayPal order id")
                    return PaymentWebhookOutcome(
                        handled=True,
                        event_id=event_id,
                        event_type=event_type,
                    )
                capture = await self.confirm_payment(paypal_order_id)
                if not capture.get("success"):
                    warning(
                        "PayPal",
                        "capture after APPROVED failed for {}: {}",
                        paypal_order_id,
                        capture.get("error"),
                    )
                return PaymentWebhookOutcome(
                    handled=True,
                    event_id=event_id,
                    event_type=event_type,
                    mark_paid=False,
                    payment_id=paypal_order_id,
                )

            if event_type == "PAYMENT.CAPTURE.COMPLETED":
                order_id = _resolve_internal_order_id(event_data)
                capture_id = event_data.get("id")
                paypal_order_id = (
                    event_data.get("supplementary_data", {})
                    .get("related_ids", {})
                    .get("order_id")
                )
                return PaymentWebhookOutcome(
                    handled=True,
                    event_id=event_id,
                    event_type=event_type,
                    mark_paid=order_id is not None,
                    order_id=order_id,
                    payment_id=str(paypal_order_id) if paypal_order_id else (
                        str(capture_id) if capture_id else None
                    ),
                    payment_charge_id=str(capture_id) if capture_id else None,
                )

            if event_type in (
                "PAYMENT.CAPTURE.DENIED",
                "PAYMENT.CAPTURE.PENDING",
                "CHECKOUT.PAYMENT-APPROVAL.REVERSED",
            ):
                warning("PayPal", "Non-success payment event: {}", event_type)
                return PaymentWebhookOutcome(
                    handled=True,
                    event_id=event_id,
                    event_type=event_type,
                    mark_paid=False,
                    order_id=_resolve_internal_order_id(event_data),
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
