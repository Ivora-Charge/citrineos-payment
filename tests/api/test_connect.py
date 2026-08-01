import os
import unittest
from unittest.mock import patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from config import Config
from db.init_db import get_db
from api.endpoints.connect import router as connect_router

TEST_SECRET = "test-sync-secret"

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app = FastAPI()
app.include_router(connect_router, prefix="/api/connect")


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

# A fully-populated Standard account as stripe.Account.retrieve returns it
# (dict access via .get, same as the endpoint uses).
FULL_ACCOUNT = {
    "id": "acct_123",
    "charges_enabled": True,
    "details_submitted": True,
    "email": "owner@chargeco.example",
    "country": "US",
    "default_currency": "usd",
    "requirements": {"disabled_reason": None, "past_due": [], "currently_due": []},
    "business_profile": {
        "name": "ChargeCo",
        "url": "https://chargeco.example",
        "support_email": "help@chargeco.example",
        "support_phone": "+15550100",
        "support_address": {
            "line1": "1 Main St",
            "line2": "Suite 2",
            "city": "San Diego",
            "state": "CA",
            "postal_code": "92101",
            "country": "US",
        },
    },
}


class ConnectStatusTests(unittest.TestCase):
    def setUp(self):
        Config.PAYMENT_CATALOG_SYNC_SECRET = TEST_SECRET
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE IF EXISTS "Tenants"'))
            conn.execute(
                text('CREATE TABLE "Tenants" (id INTEGER PRIMARY KEY, "stripeAccountId" TEXT)')
            )
            conn.execute(
                text('INSERT INTO "Tenants" (id, "stripeAccountId") VALUES (1, :a), (2, NULL)'),
                {"a": "acct_123"},
            )

    def get_status(self, tenant_id=1):
        return client.get(
            f"/api/connect/status?tenant_id={tenant_id}",
            headers={"X-Catalog-Sync-Secret": TEST_SECRET},
        )

    def test_profile_returned_from_business_profile(self):
        with patch("stripe.Account.retrieve", return_value=FULL_ACCOUNT):
            res = self.get_status()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["charges_enabled"])
        profile = body["profile"]
        self.assertEqual(profile["business_name"], "ChargeCo")
        self.assertEqual(profile["url"], "https://chargeco.example")
        self.assertEqual(profile["support_email"], "help@chargeco.example")
        self.assertEqual(profile["support_phone"], "+15550100")
        self.assertEqual(profile["address_line1"], "1 Main St")
        self.assertEqual(profile["address_line2"], "Suite 2")
        self.assertEqual(profile["address_city"], "San Diego")
        self.assertEqual(profile["address_state"], "CA")
        self.assertEqual(profile["address_postal_code"], "92101")
        self.assertEqual(profile["address_country"], "US")
        self.assertEqual(profile["email"], "owner@chargeco.example")
        self.assertEqual(profile["country"], "US")
        self.assertEqual(profile["default_currency"], "usd")

    def test_profile_all_none_when_stripe_has_no_public_profile(self):
        # business_profile and its support_address are optional in Stripe
        # onboarding -- the endpoint must not crash on their absence.
        bare = {
            "id": "acct_123",
            "charges_enabled": False,
            "details_submitted": False,
            "requirements": None,
            "business_profile": None,
        }
        with patch("stripe.Account.retrieve", return_value=bare):
            res = self.get_status()
        self.assertEqual(res.status_code, 200)
        profile = res.json()["profile"]
        self.assertIsNotNone(profile)
        self.assertTrue(all(v is None for v in profile.values()))

    def test_no_account_means_no_profile(self):
        res = self.get_status(tenant_id=2)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIsNone(body["stripe_account_id"])
        self.assertIsNone(body["profile"])

    def test_unknown_tenant_404(self):
        res = self.get_status(tenant_id=99)
        self.assertEqual(res.status_code, 404)

    def test_forged_secret_rejected(self):
        res = client.get(
            "/api/connect/status?tenant_id=1",
            headers={"X-Catalog-Sync-Secret": "wrong"},
        )
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
