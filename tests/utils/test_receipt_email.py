import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from config import Config
from db.init_db import Checkout, Connector, Evse, Location, Operator, Tariff
from schemas.checkouts import Pricing
from utils import receipt_email
from utils.receipt_email import (
    build_receipt_text,
    receipt_email_enabled,
    send_receipt_email,
)


def a_pricing(**overrides) -> Pricing:
    defaults = {
        "currency": "USD",
        "tax_rate": 10,
        "payment_fee": 3,
        "energy_consumption_kwh": 20.0,
        "energy_costs": 600,
        "time_consumption_min": 30.0,
        "time_costs": None,
        "session_costs": 100,
        "total_costs_net": 700,
        "tax_costs": 70,
        "total_costs_gross": 770,
        "payment_costs_gross": 21,
        "payment_costs_net": 21,
        "total_due": 791,
    }
    defaults.update(overrides)
    return Pricing(**defaults)


def a_checkout(**overrides) -> Checkout:
    defaults = {
        "id": 7,
        "payment_intent_id": "pi_test_123",
        "connector_id": 1,
        "tariff_id": 3,
        "customer_email": "driver@example.com",
        "captured_amount": 791,
        "overage_amount": None,
        "transaction_start_time": datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        "transaction_end_time": datetime(2026, 8, 1, 10, 30, tzinfo=timezone.utc),
        "transaction_kwh": 20.0,
    }
    defaults.update(overrides)
    return Checkout(**defaults)


TARIFF = Tariff(
    id=3,
    price_kwh=0.30,
    price_minute=None,
    price_session=1.00,
    currency="USD",
    tax_rate=10,
    payment_fee=3,
    authorization_amount=25.0,
)
EVSE = Evse(
    id=1,
    evse_id="cp002-1",
    ocpp_evse_id=1,
    status="Available",
    station_id="cp002",
    tenant_id="1",
    location_id=1,
)
LOCATION = Location(
    id=1,
    location_id="loc-001",
    address="123 Main St",
    postal_code="94103",
    city="San Francisco",
    state="CA",
    country="USA",
    operator_id=1,
)
OPERATOR = Operator(id=1, name="Test Operator", stripe_account_id="platform")


def a_db(connector_evse_id=1):
    """Mock db whose query(Model).filter(...).first() resolves the catalog
    chain used by send_receipt_email."""
    connector = Connector(id=1, evse_id=connector_evse_id, tariff_id=3)
    mapping = {
        Connector: connector,
        Evse: EVSE,
        Location: LOCATION,
        Operator: OPERATOR,
        Tariff: TARIFF,
    }
    return Mock(
        query=Mock(
            side_effect=lambda model: MagicMock(
                filter=lambda *a, **k: MagicMock(first=lambda: mapping.get(model))
            )
        )
    )


class ReceiptContentTests(unittest.TestCase):
    def test_receipt_includes_required_representations(self):
        # HB 44 3.40 recorded representations: physical location, session
        # times, quantity, itemized prices, and the total actually billed.
        text = build_receipt_text(
            a_checkout(), a_pricing(), TARIFF, EVSE, LOCATION, OPERATOR
        )
        self.assertIn("123 Main St", text)
        self.assertIn("San Francisco", text)
        self.assertIn("cp002-1", text)
        self.assertIn("20.00 kWh", text)
        self.assertIn("0.30 USD/kWh", text)
        self.assertIn("Session fee: 1.00 USD", text)
        self.assertIn("Tax (10%): 0.70 USD", text)
        self.assertIn("Transaction fee (3%): 0.21 USD", text)
        self.assertIn("Total: 7.91 USD", text)
        self.assertIn("pi_test_123", text)

    def test_receipt_shows_charged_amount_when_capped_at_hold(self):
        checkout = a_checkout(captured_amount=500, overage_amount=None)
        text = build_receipt_text(
            checkout, a_pricing(), TARIFF, EVSE, LOCATION, OPERATOR
        )
        self.assertIn("Amount charged to card: 5.00 USD", text)

    def test_receipt_omits_charged_line_when_collected_matches_total(self):
        text = build_receipt_text(
            a_checkout(), a_pricing(), TARIFF, EVSE, LOCATION, OPERATOR
        )
        self.assertNotIn("Amount charged to card", text)

    def test_receipt_omits_fee_line_when_no_fee(self):
        pricing = a_pricing(payment_fee=0, payment_costs_gross=0, total_due=770)
        text = build_receipt_text(
            a_checkout(), pricing, TARIFF, EVSE, LOCATION, OPERATOR
        )
        self.assertNotIn("Transaction fee", text)


class SendReceiptEmailTests(unittest.TestCase):
    def test_disabled_without_any_transport_config(self):
        with patch.object(Config, "RESEND_API_KEY", ""), patch.object(
            Config, "SMTP_HOST", ""
        ), patch.object(Config, "SMTP_FROM", ""):
            self.assertFalse(receipt_email_enabled())
            with patch.object(receipt_email, "smtplib") as smtplib_mock, patch.object(
                receipt_email, "requests"
            ) as requests_mock:
                send_receipt_email(a_db(), a_checkout(), a_pricing())
                smtplib_mock.SMTP.assert_not_called()
                requests_mock.post.assert_not_called()

    def test_skips_checkouts_without_customer_email(self):
        with patch.object(Config, "RESEND_API_KEY", ""), patch.object(
            Config, "SMTP_HOST", "mail.test"
        ), patch.object(Config, "SMTP_FROM", "receipts@test"):
            with patch.object(receipt_email, "smtplib") as smtplib_mock:
                send_receipt_email(
                    a_db(), a_checkout(customer_email=None), a_pricing()
                )
                smtplib_mock.SMTP.assert_not_called()

    def test_sends_receipt_via_smtp(self):
        with patch.object(Config, "RESEND_API_KEY", ""), patch.object(
            Config, "SMTP_HOST", "mail.test"
        ), patch.object(Config, "SMTP_FROM", "receipts@test"):
            with patch.object(receipt_email, "smtplib") as smtplib_mock:
                smtp = smtplib_mock.SMTP.return_value.__enter__.return_value
                send_receipt_email(a_db(), a_checkout(), a_pricing())
                smtplib_mock.SMTP.assert_called_once()
                smtp.send_message.assert_called_once()
                message = smtp.send_message.call_args[0][0]
                self.assertEqual(message["To"], "driver@example.com")
                self.assertIn("7.91 USD", message["Subject"])

    def test_sends_receipt_via_resend_api(self):
        with patch.object(Config, "RESEND_API_KEY", "re_test_key"), patch.object(
            Config, "SMTP_HOST", ""
        ), patch.object(Config, "SMTP_FROM", "Ivora <receipts@test>"):
            self.assertTrue(receipt_email_enabled())
            with patch.object(receipt_email, "requests") as requests_mock, patch.object(
                receipt_email, "smtplib"
            ) as smtplib_mock:
                send_receipt_email(a_db(), a_checkout(), a_pricing())
                smtplib_mock.SMTP.assert_not_called()
                requests_mock.post.assert_called_once()
                args, kwargs = requests_mock.post.call_args
                self.assertEqual(args[0], receipt_email.RESEND_API_URL)
                self.assertEqual(kwargs["json"]["to"], ["driver@example.com"])
                self.assertEqual(kwargs["json"]["from"], "Ivora <receipts@test>")
                self.assertIn("7.91 USD", kwargs["json"]["subject"])
                self.assertIn("123 Main St", kwargs["json"]["text"])
                self.assertEqual(
                    kwargs["headers"]["Authorization"], "Bearer re_test_key"
                )

    def test_resend_takes_precedence_over_smtp(self):
        with patch.object(Config, "RESEND_API_KEY", "re_test_key"), patch.object(
            Config, "SMTP_HOST", "mail.test"
        ), patch.object(Config, "SMTP_FROM", "receipts@test"):
            with patch.object(receipt_email, "requests") as requests_mock, patch.object(
                receipt_email, "smtplib"
            ) as smtplib_mock:
                send_receipt_email(a_db(), a_checkout(), a_pricing())
                requests_mock.post.assert_called_once()
                smtplib_mock.SMTP.assert_not_called()

    def test_send_failure_is_swallowed(self):
        # Settlement already happened; a dead mailer must never raise back
        # into the capture path -- both transports.
        with patch.object(Config, "RESEND_API_KEY", ""), patch.object(
            Config, "SMTP_HOST", "mail.test"
        ), patch.object(Config, "SMTP_FROM", "receipts@test"):
            with patch.object(receipt_email, "smtplib") as smtplib_mock:
                smtplib_mock.SMTP.side_effect = OSError("connection refused")
                send_receipt_email(a_db(), a_checkout(), a_pricing())  # no raise
        with patch.object(Config, "RESEND_API_KEY", "re_test_key"), patch.object(
            Config, "SMTP_FROM", "receipts@test"
        ):
            with patch.object(receipt_email, "requests") as requests_mock:
                requests_mock.post.side_effect = OSError("api down")
                send_receipt_email(a_db(), a_checkout(), a_pricing())  # no raise


if __name__ == "__main__":
    unittest.main()
