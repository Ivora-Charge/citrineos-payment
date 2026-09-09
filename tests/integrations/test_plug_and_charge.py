"""Per-EVSE plug-and-charge opt-in vs the prepayment stop policy.

SCAN_AND_CHARGE_REQUIRE_PREPAYMENT force-stops sessions that start without
authorization (cable plug-in autostart). An EVSE with plug_and_charge enabled
must be exempt: the session keeps running and gets the pay-while-charging
transaction QR instead.
"""

import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config
from db.init_db import (
    Base,
    Checkout,
    Connector,
    Evse,
    Location,
    Operator,
    Tariff,
)
from integrations.citrineos.citrineos import (
    CitrineOSIntegration,
    CitrineOSeventHeaders,
)
from schemas.transaction_event import (
    TransactionEventEnumType,
    TransactionEventRequest,
    TransactionType,
    TriggerReasonEnumType,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    """Run on a private loop WITHOUT touching the thread's current loop --
    asyncio.run() would unset it and break the older get_event_loop()-based
    tests that run after this module."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class PlugAndChargeGuardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        operator = Operator(id=1, name="Op", stripe_account_id="acct_1")
        location = Location(id=1, location_id="loc-1", operator_id=1)
        tariff = Tariff(
            id=1,
            price_kwh=0.30,
            currency="usd",
            tax_rate=0.0,
            authorization_amount=25.0,
            payment_fee=0.0,
            stripe_price_id="price_1",
        )
        self.evse = Evse(
            id=1,
            evse_id="cp010-1",
            ocpp_evse_id=1,
            status="Available",
            station_id="cp010",
            tenant_id="1",
            location_id=1,
        )
        connector = Connector(
            id=1,
            connector_id="cp010-1-1",
            power_type="AC_1_PHASE",
            max_voltage=240,
            max_amperage=32,
            evse_id=1,
            tariff_id=1,
        )
        self.db.add_all([operator, location, tariff, self.evse, connector])
        self.db.commit()

        self.integration = CitrineOSIntegration.__new__(CitrineOSIntegration)
        self.integration.send_citrineos_message = MagicMock()
        self.integration._ensure_stripe_price = MagicMock()
        self.integration.create_payment_link = AsyncMock(
            return_value="https://pay.example/link"
        )
        self.integration._resolve_display_adapter_type = MagicMock()

        self.headers = CitrineOSeventHeaders(stationId="cp010", tenantId="1")
        self.event = TransactionEventRequest(
            eventType=TransactionEventEnumType.Started,
            timestamp=NOW,
            triggerReason=TriggerReasonEnumType.CablePluggedIn,
            transactionInfo=TransactionType(transactionId="555"),
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _run(self):
        adapter = MagicMock()
        adapter.show_transaction_qr = AsyncMock(return_value=1)
        with (
            patch(
                "integrations.citrineos.citrineos.get_db",
                side_effect=lambda: iter([self.db]),
            ),
            patch(
                "integrations.citrineos.citrineos.get_display_adapter",
                return_value=adapter,
            ),
            patch.object(Config, "SCAN_AND_CHARGE_REQUIRE_PREPAYMENT", True),
        ):
            run(
                self.integration.process_transaction_started_scan_and_charge(
                    transaction_event=self.event,
                    citrine_os_event_headers=self.headers,
                )
            )

    def _stop_calls(self):
        return [
            call
            for call in self.integration.send_citrineos_message.call_args_list
            if call.kwargs.get("url_path") == "evdriver/requestStopTransaction"
        ]

    def test_prepayment_policy_stops_unauthorized_session(self):
        self._run()

        stops = self._stop_calls()
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].kwargs["json_payload"], {"transactionId": "555"})
        self.assertEqual(self.db.query(Checkout).count(), 0)

    def test_plug_and_charge_evse_is_exempt(self):
        self.evse.plug_and_charge = True
        self.db.commit()

        self._run()

        self.assertEqual(self._stop_calls(), [])
        # The session runs and enters the pay-while-charging flow: a checkout
        # exists and the transaction QR was pushed.
        checkout = self.db.query(Checkout).one()
        self.assertEqual(checkout.qr_code_message_id, 1)
        self.assertEqual(checkout.platform_fee_bps, 1000)
        self.integration.create_payment_link.assert_awaited_once()


class PlugAndChargeAuthorizationSyncTests(unittest.TestCase):
    """sync_plug_and_charge_authorization keeps the tenant's FFFFFFFF
    whitelist row in the core Authorizations table in step with the per-EVSE
    opt-in."""

    def setUp(self):
        from sqlalchemy import text

        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        # The payment models don't declare the core Authorizations table;
        # create the columns the sync touches, with the real unique key.
        self.db.execute(
            text(
                'CREATE TABLE "Authorizations" ('
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                '"idToken" TEXT NOT NULL, '
                '"idTokenType" TEXT, '
                "status TEXT NOT NULL DEFAULT 'Accepted', "
                '"concurrentTransaction" BOOLEAN, '
                '"tenantId" INTEGER NOT NULL, '
                '"createdAt" TIMESTAMP, '
                '"updatedAt" TIMESTAMP, '
                'UNIQUE ("tenantId", "idToken", "idTokenType"))'
            )
        )
        self.evse = Evse(
            id=1,
            evse_id="R1-1",
            ocpp_evse_id=1,
            status="Available",
            station_id="R1",
            tenant_id="3",
            display_adapter_type="renova",
            plug_and_charge=True,
        )
        self.db.add(self.evse)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _rows(self):
        from sqlalchemy import text

        return self.db.execute(
            text(
                'SELECT "idToken", status, "concurrentTransaction", "tenantId" '
                'FROM "Authorizations"'
            )
        ).fetchall()

    def _sync(self):
        from integrations import rcd_vendor

        run(
            rcd_vendor.sync_plug_and_charge_authorization(
                MagicMock(), self.db, self.evse
            )
        )

    def test_opted_in_rcd_evse_whitelists_tag(self):
        self._sync()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        token, status, concurrent, tenant = rows[0]
        self.assertEqual(token, "FFFFFFFF")
        self.assertEqual(status, "Accepted")
        self.assertTrue(concurrent)
        self.assertEqual(tenant, 3)

    def test_sync_is_idempotent(self):
        self._sync()
        self._sync()
        self.assertEqual(len(self._rows()), 1)

    def test_disabling_last_evse_revokes_tag(self):
        self._sync()
        self.evse.plug_and_charge = False
        self.db.commit()
        self._sync()
        self.assertEqual(self._rows(), [])

    def test_row_survives_while_another_evse_is_opted_in(self):
        other = Evse(
            id=2,
            evse_id="R1-2",
            ocpp_evse_id=2,
            status="Available",
            station_id="R1",
            tenant_id="3",
            display_adapter_type="renova",
            plug_and_charge=True,
        )
        self.db.add(other)
        self.db.commit()
        self._sync()
        self.evse.plug_and_charge = False
        self.db.commit()
        self._sync()
        self.assertEqual(len(self._rows()), 1)

    def test_non_rcd_evse_is_a_noop(self):
        self.evse.display_adapter_type = "standard"
        self.db.commit()
        self._sync()
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
