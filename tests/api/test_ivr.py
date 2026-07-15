import os
import unittest
from base64 import b64encode
from hashlib import sha1
from hmac import new as hmac_new
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

import stripe
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config import Config
from db.init_db import (
    Base,
    get_db,
    Checkout,
    Connector,
    Evse,
    Location,
    Operator,
    Tariff,
)
from catalog.phone_codes import backfill_phone_codes, ensure_phone_code
from api.endpoints.ivr import router as ivr_router

TEST_TOKEN = "test-twilio-token"

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeCitrineResponse:
    status_code = 200

    def json(self):
        return [{"success": True}]


class FakeOcpp:
    """The two calls handle_web_portal makes, recorded."""

    def __init__(self):
        self.sent = []
        self.authorizations = []

    async def create_authorization(self, id_token, token_type, additional_info):
        self.authorizations.append((id_token, token_type, additional_info))
        return {"idToken": {"idToken": id_token, "type": "Central"}}

    def send_citrineos_message(self, *, station_id, tenant_id, url_path, json_payload):
        self.sent.append(
            {
                "station_id": station_id,
                "tenant_id": tenant_id,
                "url_path": url_path,
                "json_payload": json_payload,
            }
        )
        return FakeCitrineResponse()


app = FastAPI()
app.include_router(ivr_router, prefix="/api/ivr")


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def twilio_signature(url: str, params: dict) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    return b64encode(
        hmac_new(TEST_TOKEN.encode(), payload.encode(), sha1).digest()
    ).decode()


def post(path: str, data: dict | None = None, signature: str | None = None):
    data = data or {}
    if signature is None:
        signature = twilio_signature("http://testserver" + path, data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": signature})


def seed_catalog(db):
    operator = Operator(name="Platform Op", stripe_account_id="platform")
    connected = Operator(name="Connect Op", stripe_account_id="acct_123")
    db.add_all([operator, connected])
    db.flush()
    loc1 = Location(
        location_id="loc-1",
        address="1 Main St",
        city="Testville",
        operator_id=operator.id,
    )
    loc2 = Location(
        location_id="loc-2",
        address="2 Side St",
        city="Testville",
        operator_id=connected.id,
    )
    db.add_all([loc1, loc2])
    db.flush()
    tariff = Tariff(
        price_kwh=0.45,
        price_minute=0.0,
        price_session=0.0,
        currency="usd",
        tax_rate=0.0,
        authorization_amount=25.0,
        payment_fee=0.0,
    )
    db.add(tariff)
    db.flush()
    evse1 = Evse(
        evse_id="CP1-1",
        ocpp_evse_id=1,
        status="Available",
        station_id="CP1",
        tenant_id="1",
        location_id=loc1.id,
        phone_code="123456",
    )
    evse2 = Evse(
        evse_id="CP2-1",
        ocpp_evse_id=1,
        status="Available",
        station_id="CP2",
        tenant_id="1",
        location_id=loc2.id,
        phone_code="654321",
    )
    db.add_all([evse1, evse2])
    db.flush()
    db.add_all(
        [
            Connector(
                connector_id="CP1-1-1",
                power_type="AC_1_PHASE",
                max_voltage=240,
                max_amperage=32,
                evse_id=evse1.id,
                tariff_id=tariff.id,
            ),
            Connector(
                connector_id="CP2-1-1",
                power_type="AC_1_PHASE",
                max_voltage=240,
                max_amperage=32,
                evse_id=evse2.id,
                tariff_id=tariff.id,
            ),
        ]
    )
    db.commit()


class IvrTestCase(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        db = TestSession()
        seed_catalog(db)
        db.close()
        Config.TWILIO_AUTH_TOKEN = TEST_TOKEN
        Config.IVR_PUBLIC_BASE_URL = ""
        Config.TWILIO_PAY_CONNECTOR = ""
        Config.IVR_SUPPORT_PHONE = "+18005551234"
        app.ocpp_integration = FakeOcpp()

    def db(self):
        return TestSession()


class SignatureTests(IvrTestCase):
    def test_no_token_fails_closed(self):
        Config.TWILIO_AUTH_TOKEN = ""
        response = post("/api/ivr/voice")
        self.assertEqual(response.status_code, 503)

    def test_bad_signature_rejected(self):
        response = post("/api/ivr/voice", signature="bogus")
        self.assertEqual(response.status_code, 403)

    def test_signature_covers_post_params(self):
        # Signature computed over different params than those sent.
        signature = twilio_signature(
            "http://testserver/api/ivr/code", {"Digits": "111111"}
        )
        response = post("/api/ivr/code", {"Digits": "123456"}, signature=signature)
        self.assertEqual(response.status_code, 403)

    def test_public_base_url_used_when_configured(self):
        Config.IVR_PUBLIC_BASE_URL = "https://pay.example.com"
        signature = twilio_signature("https://pay.example.com/api/ivr/voice", {})
        response = client.post(
            "/api/ivr/voice", data={}, headers={"X-Twilio-Signature": signature}
        )
        self.assertEqual(response.status_code, 200)


class VoiceTests(IvrTestCase):
    def test_greets_with_bot_disclosure_and_gathers_code(self):
        response = post("/api/ivr/voice")
        self.assertEqual(response.status_code, 200)
        self.assertIn("automated assistant", response.text)
        self.assertIn('action="code?attempt=1"', response.text)
        self.assertIn('finishOnKey="#"', response.text)

    def test_no_input_retries_without_greeting_then_gives_up(self):
        second = post("/api/ivr/voice?attempt=2")
        self.assertNotIn("automated assistant", second.text)
        self.assertIn("voice?attempt=3", second.text)
        third = post("/api/ivr/voice?attempt=3")
        self.assertNotIn("<Redirect", third.text)
        self.assertIn("Goodbye", third.text)


class CodeTests(IvrTestCase):
    def test_valid_code_reads_price_and_hold(self):
        response = post("/api/ivr/code", {"Digits": "123456"})
        text = response.text
        self.assertIn("1 Main St", text)
        self.assertIn("45 cents per kilowatt hour", text)
        self.assertIn("25 dollars", text)
        db = self.db()
        evse_pk = db.query(Evse).filter(Evse.phone_code == "123456").first().id
        db.close()
        self.assertIn(f'action="confirm?evse={evse_pk}"', text)

    def test_unknown_code_retries_then_gives_up(self):
        response = post("/api/ivr/code?attempt=1", {"Digits": "999999"})
        self.assertIn("voice?attempt=2", response.text)
        final = post("/api/ivr/code?attempt=3", {"Digits": "999999"})
        self.assertNotIn("<Redirect", final.text)
        self.assertIn("Goodbye", final.text)

    def test_zero_dials_support(self):
        response = post("/api/ivr/code", {"Digits": "0"})
        self.assertIn("<Dial>+18005551234</Dial>", response.text)

    def test_busy_connector_is_refused(self):
        db = self.db()
        connector = db.query(Connector).filter_by(connector_id="CP1-1-1").first()
        db.add(
            Checkout(
                connector_id=connector.id,
                tariff_id=connector.tariff_id,
                transaction_start_time=__import__("datetime").datetime.now(),
            )
        )
        db.commit()
        db.close()
        response = post("/api/ivr/code", {"Digits": "123456"})
        self.assertIn("currently in use", response.text)
        self.assertNotIn("<Gather", response.text)


class ConfirmTests(IvrTestCase):
    def evse_pk(self, code):
        db = self.db()
        pk = db.query(Evse).filter(Evse.phone_code == code).first().id
        db.close()
        return pk

    def test_press_1_creates_ivr_checkout_and_pays_tokenize_mode(self):
        response = post(
            f"/api/ivr/confirm?evse={self.evse_pk('123456')}", {"Digits": "1"}
        )
        db = self.db()
        checkout = db.query(Checkout).first()
        db.close()
        self.assertIsNotNone(checkout)
        self.assertEqual(checkout.source, "ivr")
        self.assertIn('tokenType="reusable"', response.text)
        self.assertNotIn("chargeAmount", response.text)
        self.assertIn(f'action="pay?checkout={checkout.id}"', response.text)
        # Platform operator + no configured connector => Twilio default.
        self.assertNotIn("paymentConnector", response.text)

    def test_connect_operator_selects_connector_named_after_account(self):
        response = post(
            f"/api/ivr/confirm?evse={self.evse_pk('654321')}", {"Digits": "1"}
        )
        self.assertIn('paymentConnector="acct_123"', response.text)

    def test_press_2_asks_for_new_code(self):
        response = post(
            f"/api/ivr/confirm?evse={self.evse_pk('123456')}", {"Digits": "2"}
        )
        self.assertIn('action="code?attempt=1"', response.text)
        db = self.db()
        self.assertEqual(db.query(Checkout).count(), 0)
        db.close()


class PayTests(IvrTestCase):
    def make_checkout(self, connector_id="CP1-1-1"):
        db = self.db()
        connector = db.query(Connector).filter_by(connector_id=connector_id).first()
        checkout = Checkout(
            connector_id=connector.id, tariff_id=connector.tariff_id, source="ivr"
        )
        db.add(checkout)
        db.commit()
        db.refresh(checkout)
        db.close()
        return checkout.id

    def test_success_places_manual_hold_and_starts_charging(self):
        checkout_id = self.make_checkout()
        with patch(
            "api.endpoints.ivr.stripe.PaymentIntent.create",
            return_value=SimpleNamespace(id="pi_test", status="requires_capture"),
        ) as create:
            response = post(
                f"/api/ivr/pay?checkout={checkout_id}",
                {"Result": "success", "PaymentToken": "pm_test"},
            )
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["amount"], 2500)
        self.assertEqual(kwargs["capture_method"], "manual")
        self.assertEqual(kwargs["payment_method"], "pm_test")
        self.assertNotIn("stripe_account", kwargs)  # platform operator
        self.assertIn("Payment accepted", response.text)
        self.assertIn("plug in", response.text)

        sent = app.ocpp_integration.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["url_path"], "evdriver/requestStartTransaction")
        self.assertEqual(sent[0]["station_id"], "CP1")
        self.assertEqual(sent[0]["json_payload"]["remoteStartId"], checkout_id)

        db = self.db()
        checkout = db.query(Checkout).filter(Checkout.id == checkout_id).first()
        self.assertEqual(checkout.payment_intent_id, "pi_test")
        self.assertEqual(checkout.remote_request_status, "Accepted")
        db.close()

    def test_connect_operator_charges_on_connected_account(self):
        checkout_id = self.make_checkout("CP2-1-1")
        with patch(
            "api.endpoints.ivr.stripe.PaymentIntent.create",
            return_value=SimpleNamespace(id="pi_test", status="requires_capture"),
        ) as create:
            post(
                f"/api/ivr/pay?checkout={checkout_id}",
                {"Result": "success", "PaymentToken": "pm_test"},
            )
        self.assertEqual(create.call_args.kwargs["stripe_account"], "acct_123")

    def test_declined_card_offers_restart(self):
        checkout_id = self.make_checkout()
        with patch(
            "api.endpoints.ivr.stripe.PaymentIntent.create",
            side_effect=stripe.error.CardError("declined", None, "card_declined"),
        ):
            response = post(
                f"/api/ivr/pay?checkout={checkout_id}",
                {"Result": "success", "PaymentToken": "pm_test"},
            )
        self.assertIn("declined", response.text)
        self.assertIn("voice?attempt=1", response.text)
        self.assertEqual(app.ocpp_integration.sent, [])

    def test_requires_action_cancels_hold(self):
        checkout_id = self.make_checkout()
        with patch(
            "api.endpoints.ivr.stripe.PaymentIntent.create",
            return_value=SimpleNamespace(id="pi_3ds", status="requires_action"),
        ), patch("api.endpoints.ivr.stripe.PaymentIntent.cancel") as cancel:
            response = post(
                f"/api/ivr/pay?checkout={checkout_id}",
                {"Result": "success", "PaymentToken": "pm_test"},
            )
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], "pi_3ds")
        self.assertIn("cannot be used", response.text)
        self.assertEqual(app.ocpp_integration.sent, [])

    def test_pay_failure_restarts_flow(self):
        checkout_id = self.make_checkout()
        response = post(
            f"/api/ivr/pay?checkout={checkout_id}",
            {"Result": "payment-connector-error"},
        )
        self.assertIn("could not be completed", response.text)
        self.assertIn("voice?attempt=1", response.text)

    def test_hangup_returns_empty_response(self):
        checkout_id = self.make_checkout()
        response = post(
            f"/api/ivr/pay?checkout={checkout_id}", {"Result": "caller-hung-up"}
        )
        self.assertEqual(response.text.split("?>")[1], "<Response></Response>")


class PhoneCodeTests(IvrTestCase):
    def test_ensure_is_idempotent_and_unique(self):
        db = self.db()
        evse = db.query(Evse).filter(Evse.phone_code == "123456").first()
        self.assertEqual(ensure_phone_code(db, evse), "123456")
        other = Evse(
            evse_id="CP3-1",
            ocpp_evse_id=1,
            status="Available",
            station_id="CP3",
            tenant_id="1",
        )
        db.add(other)
        db.flush()
        code = ensure_phone_code(db, other)
        self.assertRegex(code, r"^[1-9]\d{5}$")
        self.assertNotEqual(code, "123456")
        db.close()

    def test_backfill_assigns_only_missing(self):
        db = self.db()
        db.add(
            Evse(
                evse_id="CP4-1",
                ocpp_evse_id=1,
                status="Available",
                station_id="CP4",
                tenant_id="1",
            )
        )
        db.commit()
        self.assertEqual(backfill_phone_codes(db), 1)
        self.assertEqual(backfill_phone_codes(db), 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
