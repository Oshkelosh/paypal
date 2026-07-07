"""Minimal unit tests for the paypal addon."""

from app.addons.payments.paypal.addon import PayPalAddon


def test_addon_identity():
    assert PayPalAddon.addon_id == "paypal"
    assert PayPalAddon.addon_category == "payment"
