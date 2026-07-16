"""Unit tests for the paypal addon payment/webhook flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.addons.payments.paypal.addon import (
    PayPalAddon,
    _resolve_internal_order_id,
)


def test_addon_identity():
    assert PayPalAddon.addon_id == "paypal"
    assert PayPalAddon.addon_category == "payment"


def test_resolve_order_id_from_purchase_units():
    resource = {
        "id": "5O190127TN364715T",
        "status": "APPROVED",
        "purchase_units": [{"custom_id": "42", "reference_id": "42"}],
    }
    assert _resolve_internal_order_id(resource) == 42


def test_resolve_order_id_from_capture_custom_id():
    resource = {
        "id": "CAPTURE123",
        "custom_id": "99",
        "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
    }
    assert _resolve_internal_order_id(resource) == 99


@pytest.mark.asyncio
async def test_parse_webhook_approved_captures_without_mark_paid():
    addon = PayPalAddon()
    addon._client_id = "cid"
    addon.confirm_payment = AsyncMock(
        return_value={"success": True, "payment_id": "5O190127TN364715T", "capture_id": "CAP1"}
    )

    payload = {
        "id": "WH-APPROVED-1",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {
            "id": "5O190127TN364715T",
            "status": "APPROVED",
            "purchase_units": [{"custom_id": "7"}],
        },
    }
    outcome = await addon.parse_webhook(payload, "")
    addon.confirm_payment.assert_awaited_once_with("5O190127TN364715T")
    assert outcome.handled is True
    assert outcome.mark_paid is False
    assert outcome.event_type == "CHECKOUT.ORDER.APPROVED"
    assert outcome.payment_id == "5O190127TN364715T"


@pytest.mark.asyncio
async def test_parse_webhook_capture_completed_marks_paid():
    addon = PayPalAddon()
    payload = {
        "id": "WH-CAPTURE-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAPTURE123",
            "custom_id": "15",
            "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
        },
    }
    outcome = await addon.parse_webhook(payload, "")
    assert outcome.handled is True
    assert outcome.mark_paid is True
    assert outcome.order_id == 15
    assert outcome.payment_charge_id == "CAPTURE123"
    assert outcome.payment_id == "5O190127TN364715T"


@pytest.mark.asyncio
async def test_parse_webhook_denied_does_not_mark_paid():
    addon = PayPalAddon()
    payload = {
        "id": "WH-DENIED-1",
        "event_type": "PAYMENT.CAPTURE.DENIED",
        "resource": {"id": "CAPTURE456", "custom_id": "3"},
    }
    outcome = await addon.parse_webhook(payload, "")
    assert outcome.handled is True
    assert outcome.mark_paid is False
    assert outcome.order_id == 3


@pytest.mark.asyncio
async def test_verify_webhook_missing_headers_returns_false():
    addon = PayPalAddon()
    addon._client_id = "cid"
    addon._webhook_id = "WH-ID"
    ok = await addon.verify_webhook(headers={}, body=b'{"id":"1"}')
    assert ok is False


@pytest.mark.asyncio
async def test_verify_webhook_success():
    addon = PayPalAddon()
    addon._client_id = "cid"
    addon._webhook_id = "WH-ID"
    addon._request = AsyncMock(return_value={"verification_status": "SUCCESS"})

    headers = {
        "paypal-transmission-id": "tid",
        "paypal-transmission-time": "2024-01-01T00:00:00Z",
        "paypal-transmission-sig": "sig",
        "paypal-cert-url": "https://api.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
    }
    ok = await addon.verify_webhook(headers=headers, body=b'{"id":"WH-1","event_type":"x"}')
    assert ok is True
    addon._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_webhook_failure_status():
    addon = PayPalAddon()
    addon._client_id = "cid"
    addon._webhook_id = "WH-ID"
    addon._request = AsyncMock(return_value={"verification_status": "FAILURE"})

    headers = {
        "paypal-transmission-id": "tid",
        "paypal-transmission-time": "2024-01-01T00:00:00Z",
        "paypal-transmission-sig": "sig",
        "paypal-cert-url": "https://api.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
    }
    ok = await addon.verify_webhook(headers=headers, body=b'{"id":"WH-1"}')
    assert ok is False
