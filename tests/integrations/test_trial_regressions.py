"""Regressions from the 2026-07-08 real-charger trial run.

The trial surfaced billing-correctness bugs around OCPP 1.6 transaction ids
(which are only unique per station) and stale AMQP redeliveries mutating
already-settled checkouts. Each test pins one of those failure modes.
"""

import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.init_db import Base, Checkout, Connector, Evse
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

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def an_event(event_type, transaction_id=None, remote_start_id=None):
    return TransactionEventRequest(
        eventType=event_type,
        timestamp=NOW,
        triggerReason=TriggerReasonEnumType.MeterValuePeriodic,
        transactionInfo=TransactionType(
            transactionId=transaction_id, remoteStartId=remote_start_id
        ),
    )


class TrialRegressionTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.integration = CitrineOSIntegration.__new__(CitrineOSIntegration)

        # Two stations, each with one EVSE/connector, mirroring the trial
        # (cp001 the simulator, R131... the real two-gun Renova).
        self.connectors = {}
        for pk, station in ((1, "cp001"), (2, "R131463260113027")):
            evse = Evse(
                id=pk,
                evse_id=f"{station}-1",
                ocpp_evse_id=1,
                status="Available",
                station_id=station,
                tenant_id="1",
            )
            connector = Connector(
                id=pk,
                connector_id=f"{station}-1-1",
                power_type="AC_1_PHASE",
                max_voltage=240,
                max_amperage=32,
                evse_id=pk,
            )
            self.db.add_all([evse, connector])
            self.connectors[station] = pk
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def a_checkout(self, station, **fields):
        checkout = Checkout(connector_id=self.connectors[station], **fields)
        self.db.add(checkout)
        self.db.commit()
        return checkout

    def patch_get_db(self):
        return patch(
            "integrations.citrineos.citrineos.get_db",
            side_effect=lambda: iter([self.db]),
        )


class FindCheckoutStationScopingTests(TrialRegressionTestBase):
    """1.6 transaction ids collide across stations (trial: 'tx 3' was live on
    cp001 AND the real charger at once); the txid match must be scoped."""

    def test_same_txid_on_two_stations_resolves_by_station(self):
        sim = self.a_checkout("cp001", remote_request_transaction_id="3")
        real = self.a_checkout(
            "R131463260113027", remote_request_transaction_id="3"
        )
        event = an_event(TransactionEventEnumType.Updated, transaction_id="3")

        found_real = self.integration.find_checkout_for_event(
            self.db, event, station_id="R131463260113027"
        )
        found_sim = self.integration.find_checkout_for_event(
            self.db, event, station_id="cp001"
        )

        self.assertEqual(found_real.id, real.id)
        self.assertEqual(found_sim.id, sim.id)

    def test_unscoped_lookup_still_matches(self):
        checkout = self.a_checkout("cp001", remote_request_transaction_id="7")
        event = an_event(TransactionEventEnumType.Updated, transaction_id="7")
        self.assertEqual(
            self.integration.find_checkout_for_event(self.db, event).id,
            checkout.id,
        )

    def test_remote_start_id_wins_regardless_of_station(self):
        checkout = self.a_checkout("cp001")
        event = an_event(
            TransactionEventEnumType.Updated, remote_start_id=checkout.id
        )
        self.assertEqual(
            self.integration.find_checkout_for_event(
                self.db, event, station_id="R131463260113027"
            ).id,
            checkout.id,
        )


class SettledCheckoutGuardTests(TrialRegressionTestBase):
    """Stale redeliveries (or a charger re-using the last PAY_<id> idTag)
    must not mutate a captured checkout — the trial rewrote checkout 150's
    start/end/kWh hours after its capture."""

    def test_started_event_ignored_after_capture(self):
        checkout = self.a_checkout(
            "cp001",
            captured_at=NOW,
            transaction_start_time=NOW,
            remote_request_transaction_id="5",
        )
        event = an_event(
            TransactionEventEnumType.Started,
            transaction_id="99",
            remote_start_id=checkout.id,
        )
        with self.patch_get_db():
            asyncio.run(
                self.integration.process_transaction_started_remote(event)
            )
        self.db.refresh(checkout)
        self.assertEqual(checkout.remote_request_transaction_id, "5")
        self.assertEqual(checkout.transaction_start_time, NOW.replace(tzinfo=None))

    def test_updated_event_ignored_after_capture(self):
        checkout = self.a_checkout(
            "cp001",
            captured_at=NOW,
            remote_request_transaction_id="5",
            transaction_kwh=1.5,
        )
        event = an_event(TransactionEventEnumType.Updated, transaction_id="5")
        with self.patch_get_db():
            asyncio.run(self.integration.process_transaction_updated(event))
        self.db.refresh(checkout)
        self.assertEqual(checkout.transaction_kwh, 1.5)

    def test_ended_event_ignored_after_capture(self):
        checkout = self.a_checkout(
            "cp001",
            captured_at=NOW,
            remote_request_transaction_id="5",
            transaction_end_time=NOW,
        )
        event = an_event(TransactionEventEnumType.Ended, transaction_id="5")
        self.integration.capture_payment_transaction = MagicMock()
        with self.patch_get_db():
            asyncio.run(self.integration.process_transaction_ended(event))
        self.integration.capture_payment_transaction.assert_not_called()


class ActiveTransactionRebindGuardTests(TrialRegressionTestBase):
    """The StartTransaction read-back must not return a transaction that is
    already bound to another checkout on the same station (trial: checkouts
    147+148 both bound to tx 3, one absorbing the other's meter values)."""

    def read_back(self, execute_row):
        fake_db = MagicMock()
        fake_db.execute.return_value.first.return_value = execute_row
        fake_db.query = self.db.query
        with patch(
            "integrations.citrineos.citrineos.get_db",
            side_effect=lambda: iter([fake_db]),
        ):
            return asyncio.run(
                self.integration._ocpp16_active_transaction_id(
                    "cp001", attempts=1
                )
            )

    def test_unbound_transaction_is_returned(self):
        self.assertEqual(self.read_back(("3",)), "3")

    def test_transaction_bound_to_another_checkout_is_not_rebound(self):
        self.a_checkout("cp001", remote_request_transaction_id="3")
        self.assertIsNone(self.read_back(("3",)))

    def test_same_txid_bound_on_other_station_does_not_block(self):
        self.a_checkout("R131463260113027", remote_request_transaction_id="3")
        self.assertEqual(self.read_back(("3",)), "3")


class LateBindStationScopingTests(TrialRegressionTestBase):
    def test_other_stations_binding_does_not_block_late_bind(self):
        self.a_checkout("cp001", remote_request_transaction_id="3")
        open_checkout = self.a_checkout(
            "R131463260113027",
            remote_request_status="Accepted",
            transaction_start_time=NOW,
        )
        with self.patch_get_db():
            self.integration._late_bind_ocpp16("3", "R131463260113027")
        self.db.refresh(open_checkout)
        self.assertEqual(open_checkout.remote_request_transaction_id, "3")

    def test_already_bound_on_same_station_is_a_noop(self):
        bound = self.a_checkout("cp001", remote_request_transaction_id="3")
        open_checkout = self.a_checkout(
            "cp001",
            remote_request_status="Accepted",
            transaction_start_time=NOW,
        )
        with self.patch_get_db():
            self.integration._late_bind_ocpp16("3", "cp001")
        self.db.refresh(open_checkout)
        self.assertIsNone(open_checkout.remote_request_transaction_id)
        self.assertEqual(bound.remote_request_transaction_id, "3")


class TransactionIdZeroTests(TrialRegressionTestBase):
    """transactionId 0 is CitrineOS's 'no transaction' sentinel and real RCD
    units stamp it on idle clock-aligned MeterValues (70 during the trial);
    it must neither late-bind nor synthesize billing events."""

    HEADERS = None  # built in setUp (needs class import)

    def setUp(self):
        super().setUp()
        self.headers = CitrineOSeventHeaders(stationId="R131463260113027")
        self.integration._late_bind_ocpp16 = MagicMock()
        self.integration.process_transaction_updated = MagicMock()
        self.integration.process_transaction_ended = MagicMock()

    def test_idle_meter_values_with_txid_zero_are_skipped(self):
        asyncio.run(
            self.integration.process_ocpp16_meter_values(
                payload={"transactionId": 0, "meterValue": []},
                citrine_os_event_headers=self.headers,
            )
        )
        self.integration._late_bind_ocpp16.assert_not_called()
        self.integration.process_transaction_updated.assert_not_called()

    def test_stop_with_txid_zero_and_no_pay_idtag_is_skipped(self):
        asyncio.run(
            self.integration.process_ocpp16_stop(
                payload={"transactionId": 0, "idTag": "AABBCCDD"},
                citrine_os_event_headers=self.headers,
            )
        )
        self.integration.process_transaction_ended.assert_not_called()

    def test_stop_with_txid_zero_but_pay_idtag_still_bills(self):
        async def fake_ended(**kwargs):
            fake_ended.called = kwargs

        fake_ended.called = None
        self.integration.process_transaction_ended = fake_ended
        asyncio.run(
            self.integration.process_ocpp16_stop(
                payload={
                    "transactionId": 0,
                    "idTag": "PAY_150",
                    "timestamp": NOW.isoformat(),
                },
                citrine_os_event_headers=self.headers,
            )
        )
        self.assertIsNotNone(fake_ended.called)
        self.assertEqual(
            fake_ended.called["transaction_event"].transactionInfo.remoteStartId,
            150,
        )
        self.integration._late_bind_ocpp16.assert_not_called()
