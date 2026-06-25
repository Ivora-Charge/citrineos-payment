import os
import unittest

os.environ.setdefault("CONFIG_PATH", ".env.test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.init_db import Base, Connector, Evse, Location, Operator, Tariff
from catalog.sync import upsert_payment_catalog


def _payload(**overrides):
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


class UpsertPaymentCatalogTests(unittest.TestCase):
    def setUp(self):
        # Fresh in-memory DB per test. The payment_* models only use the table
        # prefix from .env.test; the CitrineOS models are unused here.
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _counts(self, db):
        return {
            "operators": db.query(Operator).count(),
            "locations": db.query(Location).count(),
            "tariffs": db.query(Tariff).count(),
            "evses": db.query(Evse).count(),
            "connectors": db.query(Connector).count(),
        }

    def test_creates_full_chain(self):
        db = self.Session()
        result = upsert_payment_catalog(db, **_payload())
        db.commit()

        self.assertEqual(
            self._counts(db),
            {"operators": 1, "locations": 1, "tariffs": 1, "evses": 1, "connectors": 1},
        )
        self.assertTrue(all(result["created"].values()))
        # connector_id defaults to "{evse_id}-1"
        connector = db.query(Connector).first()
        self.assertEqual(connector.connector_id, "cp002-1-1")
        self.assertEqual(connector.tariff_id, result["tariff_id"])
        db.close()

    def test_resync_is_idempotent(self):
        db = self.Session()
        first = upsert_payment_catalog(db, **_payload())
        db.commit()

        # Re-sync the same station with an updated price -> updates in place, no
        # duplicate rows, and the tariff stays wired to the same connector.
        second = upsert_payment_catalog(db, **_payload(price_kwh=0.45))
        db.commit()

        self.assertEqual(
            self._counts(db),
            {"operators": 1, "locations": 1, "tariffs": 1, "evses": 1, "connectors": 1},
        )
        self.assertEqual(first["tariff_id"], second["tariff_id"])
        self.assertEqual(first["evse_id"], second["evse_id"])
        self.assertFalse(any(second["created"].values()))
        self.assertEqual(db.query(Tariff).first().price_kwh, 0.45)
        db.close()

    def test_second_station_gets_its_own_tariff(self):
        db = self.Session()
        upsert_payment_catalog(db, **_payload())
        db.commit()
        upsert_payment_catalog(
            db,
            **_payload(
                station_id="cp003",
                evse_id="cp003-1",
                location_id="loc-002",
                price_kwh=0.99,
            ),
        )
        db.commit()

        counts = self._counts(db)
        self.assertEqual(counts["evses"], 2)
        self.assertEqual(counts["connectors"], 2)
        # Separate tariffs -- the second station's price must not clobber the first.
        self.assertEqual(counts["tariffs"], 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
