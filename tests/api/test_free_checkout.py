"""Admin free charging (api/endpoints/checkouts.py start_free_checkout,
utils/free_charge.py): password check, throttle, zero tariff, remote start,
and Stripe-free settlement."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.endpoints.checkouts import router as checkouts_router
from catalog.sync import upsert_payment_catalog
from db.init_db import Base, Checkout, Connector, Evse, Location, Operator, Tariff, get_db
from utils import free_charge

# platform-api/free_charge.py output for "open-sesame" (same PBKDF2 format).
import base64
import hashlib

def make_hash(password: str, iterations: int = 1000) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(("pbkdf2_sha256", str(iterations), base64.b64encode(salt).decode(), base64.b64encode(digest).decode()))


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeCitrineResponse:
    def __init__(self, ok=True):
        self.status_code = 200
        self._ok = ok

    def json(self):
        return [{"success": self._ok}]


class FakeOcpp:
    def __init__(self, accept=True):
        self.accept = accept
        self.sent = []
        self.authorizations = []

    async def create_authorization(self, id_token, token_type, additional_info, tenant_id=1):
        self.authorizations.append((id_token, token_type, additional_info, tenant_id))
        return {"idToken": {"idToken": id_token, "type": token_type}}

    def send_citrineos_message(self, *, station_id, tenant_id, url_path, json_payload):
        self.sent.append({"station_id": station_id, "tenant_id": tenant_id, "url_path": url_path, "json_payload": json_payload})
        return FakeCitrineResponse(self.accept)


app = FastAPI()
app.include_router(checkouts_router, prefix="/api/checkouts")


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def seed(db, enabled=True, password="open-sesame"):
    op = Operator(name="Host Op", stripe_account_id="acct_1")
    db.add(op)
    db.flush()
    loc = Location(location_id="loc-1", address="1 Main St", city="Testville", operator_id=op.id)
    db.add(loc)
    db.flush()
    tariff = Tariff(price_kwh=0.45, price_minute=0.0, price_session=0.0, currency="usd", tax_rate=0.0, authorization_amount=25.0, payment_fee=0.0)
    db.add(tariff)
    db.flush()
    evse = Evse(evse_id="CP1-1", ocpp_evse_id=1, status="Available", station_id="CP1", tenant_id="7", location_id=loc.id,
                free_charge_enabled=enabled, free_charge_password_hash=make_hash(password) if password else None)
    db.add(evse)
    db.flush()
    db.add(Connector(connector_id="CP1-1-1", power_type="AC_1_PHASE", max_voltage=240, max_amperage=32, evse_id=evse.id, tariff_id=tariff.id))
    db.commit()
    return evse


class FreeCheckoutTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        free_charge._failures.clear()
        app.ocpp_integration = FakeOcpp()

    def test_hash_roundtrip_and_bad_formats(self):
        h = make_hash("hunter22")
        self.assertTrue(free_charge.verify_password("hunter22", h))
        self.assertFalse(free_charge.verify_password("hunter2", h))
        self.assertFalse(free_charge.verify_password("hunter22", None))
        self.assertFalse(free_charge.verify_password("hunter22", "garbage"))
        self.assertFalse(free_charge.verify_password("", h))

    def test_starts_free_session_with_zero_tariff(self):
        db = TestSession()
        seed(db)
        r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["remote_request_status"], "Accepted")
        ck = db.query(Checkout).filter(Checkout.id == r.json()["id"]).first()
        self.assertEqual(ck.source, "free")
        self.assertIsNone(ck.payment_intent_id)
        self.assertEqual(ck.authorization_amount, 0)
        tariff = db.query(Tariff).filter(Tariff.id == ck.tariff_id).first()
        self.assertEqual((tariff.price_kwh, tariff.price_minute, tariff.price_session, tariff.authorization_amount), (0, 0, 0, 0))
        self.assertEqual(tariff.currency, "usd")
        sent = app.ocpp_integration.sent[0]
        self.assertEqual(sent["url_path"], "evdriver/requestStartTransaction")
        self.assertEqual(sent["tenant_id"], "7")
        self.assertEqual(sent["json_payload"]["remoteStartId"], ck.id)
        self.assertEqual(sent["json_payload"]["evseId"], 1)
        self.assertTrue(sent["json_payload"]["idToken"]["idToken"].endswith(str(ck.id)))
        # Second free session reuses the same zero tariff.
        r2 = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        ck2 = db.query(Checkout).filter(Checkout.id == r2.json()["id"]).first()
        self.assertEqual(ck2.tariff_id, ck.tariff_id)

    def test_rejected_remote_start_is_reported(self):
        db = TestSession()
        seed(db)
        app.ocpp_integration = FakeOcpp(accept=False)
        r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["remote_request_status"], "Rejected")

    def test_wrong_password_then_lockout(self):
        db = TestSession()
        seed(db)
        for _ in range(free_charge.MAX_FAILURES):
            r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "nope"})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["detail"], "checkout.free.error.password")
        r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(db.query(Checkout).count(), 0)
        self.assertEqual(app.ocpp_integration.sent, [])

    def test_disabled_or_unknown_evse(self):
        db = TestSession()
        seed(db, enabled=False)
        r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "checkout.free.error.unavailable")
        r = client.post("/api/checkouts/free", json={"evse_id": "nope-1", "password": "x"})
        self.assertEqual(r.status_code, 404)

    def test_catalog_sync_writes_and_clears_pair(self):
        db = TestSession()
        base = dict(operator_name="Op", stripe_account_id="acct_9", location_id="loc-9", address="", postal_code="", city="", state="",
                    country="USA", station_id="CP9", tenant_id="9", ocpp_evse_id=1, evse_id="CP9-1")
        upsert_payment_catalog(db, **base, free_charge_enabled=True, free_charge_password_hash="pbkdf2_sha256$1$a$b")
        db.commit()
        evse = db.query(Evse).filter(Evse.evse_id == "CP9-1").first()
        self.assertTrue(evse.free_charge_enabled)
        self.assertEqual(evse.free_charge_password_hash, "pbkdf2_sha256$1$a$b")
        # Absent = untouched.
        upsert_payment_catalog(db, **base)
        db.commit()
        db.refresh(evse)
        self.assertTrue(evse.free_charge_enabled)
        # Disable drops the hash even if one is sent along.
        upsert_payment_catalog(db, **base, free_charge_enabled=False, free_charge_password_hash="pbkdf2_sha256$1$a$b")
        db.commit()
        db.refresh(evse)
        self.assertFalse(evse.free_charge_enabled)
        self.assertIsNone(evse.free_charge_password_hash)

    def test_evse_endpoint_exposes_flag_only(self):
        from api.endpoints.evses import router as evses_router
        app2 = FastAPI()
        app2.include_router(evses_router, prefix="/api/evses")
        app2.dependency_overrides[get_db] = override_get_db
        db = TestSession()
        seed(db)
        body = TestClient(app2).get("/api/evses/CP1-1").json()
        self.assertTrue(body["free_charge_enabled"])
        self.assertNotIn("free_charge_password_hash", body)

    def test_settlement_skips_stripe_for_free_checkout(self):
        from integrations.integration import OcppIntegration
        import asyncio
        db = TestSession()
        seed(db)
        r = client.post("/api/checkouts/free", json={"evse_id": "CP1-1", "password": "open-sesame"})
        cid = r.json()["id"]
        ck = db.query(Checkout).filter(Checkout.id == cid).first()
        ck.transaction_start_time = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
        ck.transaction_end_time = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
        ck.transaction_kwh = 12.5
        db.add(ck)
        db.commit()
        integration = OcppIntegration.__new__(OcppIntegration)
        with patch("integrations.integration.get_db", override_get_db), \
             patch("integrations.integration.stripe") as stripe_mock:
            asyncio.get_event_loop().run_until_complete(integration.capture_payment_transaction(checkout_id=cid))
            stripe_mock.PaymentIntent.retrieve.assert_not_called()
            stripe_mock.PaymentIntent.capture.assert_not_called()
        db.refresh(ck)
        self.assertIsNotNone(ck.captured_at)
        self.assertEqual(ck.captured_amount, 0)


if __name__ == "__main__":
    unittest.main()
