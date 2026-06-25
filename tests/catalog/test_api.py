import os
import unittest
from unittest.mock import patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.init_db import Base, get_db
import api.endpoints.catalog as catalog_endpoint

SECRET = "test-secret"


def _body(**overrides):
    base = dict(
        operator_name="Test Operator",
        stripe_account_id="platform",
        location_id="loc-001",
        address="1 Test St",
        postal_code="00000",
        city="Testville",
        state="TS",
        country="USA",
        station_id="cp002",
        tenant_id="1",
        ocpp_evse_id=1,
        evse_id="cp002-1",
    )
    base.update(overrides)
    return base


class CatalogApiTests(unittest.TestCase):
    def setUp(self):
        # Share one in-memory connection across threads: TestClient runs the
        # sync endpoint in a worker thread, which would otherwise get its own
        # empty SQLite database.
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(bind=self.engine)

        def override_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(catalog_endpoint.router, prefix="/api/catalog")
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self._patch = patch.object(
            catalog_endpoint.Config, "PAYMENT_CATALOG_SYNC_SECRET", SECRET
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_sync_requires_secret(self):
        r = self.client.post("/api/catalog/sync", json=_body())
        self.assertEqual(r.status_code, 401)

    def test_sync_fails_closed_without_configured_secret(self):
        with patch.object(catalog_endpoint.Config, "PAYMENT_CATALOG_SYNC_SECRET", ""):
            r = self.client.post(
                "/api/catalog/sync",
                json=_body(),
                headers={"X-Catalog-Sync-Secret": "anything"},
            )
        self.assertEqual(r.status_code, 503)

    def test_sync_creates_and_status_reports_exists(self):
        headers = {"X-Catalog-Sync-Secret": SECRET}
        r = self.client.post("/api/catalog/sync", json=_body(), headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(all(r.json()["created"].values()))

        s = self.client.get(
            "/api/catalog/status", params={"evse_id": "cp002-1"}, headers=headers
        )
        self.assertEqual(s.status_code, 200)
        self.assertTrue(s.json()["exists"])

        # Idempotent over HTTP: re-sync creates nothing new.
        r2 = self.client.post("/api/catalog/sync", json=_body(), headers=headers)
        self.assertFalse(any(r2.json()["created"].values()))

    def test_status_unknown_evse(self):
        r = self.client.get(
            "/api/catalog/status",
            params={"evse_id": "nope"},
            headers={"X-Catalog-Sync-Secret": SECRET},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["exists"])

    def test_power_type_normalizes_ac(self):
        r = self.client.post(
            "/api/catalog/sync",
            json=_body(power_type="AC"),
            headers={"X-Catalog-Sync-Secret": SECRET},
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
