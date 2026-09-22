"""Production Starx regressions: pending holds, Connect cancellation and pool leaks."""

import asyncio
import gc
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from db.init_db import Base, Checkout
from tests.api.test_free_checkout import (
    TestSession,
    engine,
    seed,
    app,
    client,
    FakeOcpp,
)
from api.endpoints.webhooks import handle_web_portal
from tasks.background import recover_payments_once
from utils.utils import generate_pricing
from utils.payment_lifecycle import cancel_checkout, reconcile_core_transaction
from integrations.integration import OcppIntegration
from integrations.citrineos.citrineos import CitrineOSIntegration, CitrineOSeventHeaders
from schemas.status_notification import StatusNotificationRequest
from schemas.transaction_event import TransactionEventRequest


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class PaymentRecoveryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db = TestSession()
        self.evse = seed(self.db)
        self.checkout = Checkout(
            connector_id=self.evse.connectors[0].id,
            tariff_id=self.evse.connectors[0].tariff_id,
            payment_intent_id="pi_pending",
            authorization_amount=2500,
            remote_request_status="Accepted",
        )
        self.db.add(self.checkout)
        self.db.commit()
        self.cid = self.checkout.id
        app.ocpp_integration = FakeOcpp()

    def tearDown(self):
        self.db.close()

    def test_stop_before_start_releases_connect_hold(self):
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            response = client.post(f"/api/checkouts/{self.cid}/stop")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(cancel.call_args.kwargs["stripe_account"], "acct_1")

    def test_rejected_start_cancels_on_connected_account(self):
        self.checkout.remote_request_status = None
        self.db.commit()
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            run(
                handle_web_portal(
                    self.db, FakeOcpp(accept=False), self.checkout, "pi_pending"
                )
            )
        self.assertEqual(cancel.call_args.kwargs.get("stripe_account"), "acct_1")

    def age(self, seconds, **fields):
        self.checkout.authorized_at = datetime.now(timezone.utc) - timedelta(
            seconds=seconds
        )
        for key, value in fields.items():
            setattr(self.checkout, key, value)
        self.db.commit()

    def recover(self):
        with (
            patch("tasks.background.get_db", side_effect=lambda: iter([TestSession()])),
            patch(
                "integrations.integration.get_db",
                side_effect=lambda: iter([TestSession()]),
            ),
        ):
            run(recover_payments_once(OcppIntegration()))
        self.db.expire_all()

    def test_no_start_hold_released_at_five_minutes_without_a_browser(self):
        self.age(299)
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            self.recover()
            cancel.assert_not_called()
            self.age(301)
            self.recover()
            self.recover()
        self.assertEqual(cancel.call_count, 1)
        self.assertIsNotNone(self.db.get(Checkout, self.cid).canceled_at)

    def test_recent_energy_activity_preserves_authorization(self):
        self.age(
            900,
            transaction_start_time=datetime.now(timezone.utc) - timedelta(minutes=15),
            transaction_kwh=1.0,
            last_activity_at=datetime.now(timezone.utc),
        )
        with patch("stripe.PaymentIntent.cancel") as cancel:
            self.recover()
        cancel.assert_not_called()

    def test_started_but_zero_energy_times_out_and_retries_stop_after_release(self):
        self.age(
            301,
            transaction_start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            transaction_kwh=0,
            remote_request_transaction_id="tx-idle",
        )
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ),
            patch.object(OcppIntegration, "send_citrineos_message") as stop,
        ):
            self.recover()
            checkout = self.db.get(Checkout, self.cid)
            self.assertIsNotNone(checkout.canceled_at)
            self.assertIsNotNone(checkout.stop_requested_at)
            self.recover()
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(
                stop.call_args.kwargs["json_payload"], {"transactionId": "tx-idle"}
            )

    def test_inactive_charging_captures_usage_and_releases_unused_hold(self):
        activity = datetime.now(timezone.utc) - timedelta(seconds=301)
        self.age(
            1200,
            transaction_start_time=activity - timedelta(minutes=10),
            transaction_kwh=2,
            last_activity_at=activity,
            remote_request_transaction_id="tx-idle",
        )
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.capture",
                return_value=SimpleNamespace(status="succeeded"),
            ) as capture,
            patch.object(OcppIntegration, "send_citrineos_message") as stop,
            patch("integrations.integration.send_receipt_email"),
        ):
            self.recover()
        self.assertEqual(capture.call_args.kwargs["amount_to_capture"], 90)
        self.assertEqual(stop.call_count, 1)
        checkout = self.db.get(Checkout, self.cid)
        self.assertEqual(
            checkout.transaction_end_time.replace(tzinfo=timezone.utc), activity
        )

    def test_cancel_before_checkout_payment_expires_stripe_session(self):
        self.checkout.payment_intent_id = None
        self.checkout.stripe_checkout_session_id = "cs_open"
        self.db.commit()
        with (
            patch(
                "stripe.checkout.Session.retrieve",
                return_value=SimpleNamespace(
                    id="cs_open", status="open", payment_intent=None
                ),
            ),
            patch(
                "stripe.checkout.Session.expire",
                return_value=SimpleNamespace(
                    id="cs_open", status="expired", payment_intent=None
                ),
            ) as expire,
        ):
            response = client.post(f"/api/checkouts/{self.cid}/stop")
        self.assertEqual(response.json()["status"], "Canceled")
        self.assertEqual(expire.call_args.kwargs["stripe_account"], "acct_1")

    def test_failed_release_is_durable_and_retried(self):
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                side_effect=RuntimeError("temporary network failure"),
            ),
        ):
            response = client.post(f"/api/checkouts/{self.cid}/stop")
        self.assertEqual(response.json()["status"], "Canceling")
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(Checkout, self.cid).cancellation_requested_at)
        self.assertIsNone(self.db.get(Checkout, self.cid).captured_at)
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ),
        ):
            self.recover()
        self.assertIsNotNone(self.db.get(Checkout, self.cid).canceled_at)

    def test_delayed_webhook_after_cancellation_never_starts_charger(self):
        self.checkout.payment_intent_id = None
        self.checkout.remote_request_status = None
        self.db.commit()
        cancel_checkout(self.db, self.cid, "driver_canceled")
        ocpp = FakeOcpp()
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            run(
                handle_web_portal(
                    self.db, ocpp, self.db.get(Checkout, self.cid), "pi_pending"
                )
            )
        self.assertEqual(ocpp.sent, [])
        self.assertEqual(cancel.call_count, 1)

    def test_completed_session_retries_failed_capture(self):
        self.age(
            3600,
            transaction_start_time=datetime.now(timezone.utc) - timedelta(minutes=50),
            transaction_end_time=datetime.now(timezone.utc) - timedelta(minutes=10),
            transaction_kwh=2.0,
        )
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.capture",
                side_effect=[
                    RuntimeError("Stripe unavailable"),
                    SimpleNamespace(status="succeeded"),
                ],
            ) as capture,
            patch("integrations.integration.send_receipt_email"),
        ):
            self.recover()
            self.assertIsNone(self.db.get(Checkout, self.cid).captured_at)
            self.recover()
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(self.db.get(Checkout, self.cid).captured_amount, 90)

    def test_preparing_then_available_releases_hold_immediately(self):
        self.age(20)
        self.evse.status = "Occupied"
        self.db.commit()
        ocpp = CitrineOSIntegration.__new__(CitrineOSIntegration)
        from unittest.mock import AsyncMock

        ocpp.push_standing_qr = AsyncMock()
        with (
            patch(
                "integrations.citrineos.citrineos.get_db",
                side_effect=lambda: iter([TestSession()]),
            ),
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            run(
                ocpp.process_status_notification(
                    StatusNotificationRequest(
                        timestamp=datetime.now(timezone.utc),
                        connectorId=1,
                        evseId=1,
                        connectorStatus="Available",
                    ),
                    CitrineOSeventHeaders(stationId="CP1", tenantId="7"),
                )
            )
        self.assertEqual(cancel.call_count, 1)

    def test_zero_meter_packets_do_not_extend_inactivity(self):
        self.age(301, transaction_kwh=0, transaction_last_meter_reading=10.0)
        ocpp = CitrineOSIntegration.__new__(CitrineOSIntegration)
        event = TransactionEventRequest(
            eventType="Updated",
            timestamp=datetime.now(timezone.utc),
            triggerReason="MeterValuePeriodic",
            transactionInfo={"transactionId": "1"},
            meterValue=[
                {
                    "timestamp": datetime.now(timezone.utc),
                    "sampledValue": [
                        {"value": 10000, "measurand": "Energy.Active.Import.Register"}
                    ],
                }
            ],
        )
        ocpp.update_checkout_with_meter_values(event, self.checkout)
        self.assertIsNone(self.checkout.last_activity_at)

    def reconcile_energy(self, start_kwh, total_kwh, timestamp):
        # Exercise the PostgreSQL-only recovery branch. Core stores meterStart
        # in kWh, whereas the charger's OCPP 1.6 meterStart/meterStop use Wh.
        core_db = Mock()
        core_db.get_bind.return_value.dialect.name = "postgresql"
        core_db.execute.return_value.first.return_value = (
            "30",
            self.checkout.transaction_start_time,
            None,
            Decimal(total_kwh),
            timestamp,
            Decimal(start_kwh),
        )
        with patch("utils.payment_lifecycle.checkout_evse", return_value=self.evse):
            reconcile_core_transaction(core_db, self.checkout)

    def test_recovered_starx_session_captures_242_cents_without_overage(self):
        start = datetime(2026, 9, 21, 20, 35, 11, tzinfo=timezone.utc)
        end = datetime(2026, 9, 21, 21, 49, 44, tzinfo=timezone.utc)
        self.checkout.transaction_start_time = start
        self.checkout.transaction_last_meter_reading = 328.828
        self.checkout.transaction_kwh = 0
        self.evse.connectors[0].tariff.price_kwh = 0.35
        self.reconcile_energy("328.828", "0.009", start + timedelta(seconds=30))

        ocpp = CitrineOSIntegration.__new__(CitrineOSIntegration)
        event = TransactionEventRequest(
            eventType="Ended",
            timestamp=end,
            triggerReason="EVDeparted",
            transactionInfo={"transactionId": "30"},
            meterValue=ocpp._ocpp16_energy_meter_value(335769),
        )
        ocpp.update_checkout_with_meter_values(event, self.checkout)
        self.checkout.transaction_end_time = end
        self.db.commit()
        with (
            patch(
                "integrations.integration.get_db",
                side_effect=lambda: iter([TestSession()]),
            ),
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.capture",
                return_value=SimpleNamespace(
                    status="succeeded",
                    customer="cus_driver",
                    payment_method="pm_driver",
                ),
            ) as capture,
            patch(
                "stripe.PaymentIntent.create",
                return_value=SimpleNamespace(id="pi_overage", status="succeeded"),
            ) as overage,
            patch("integrations.integration.send_receipt_email"),
        ):
            run(ocpp.capture_payment_transaction(checkout_id=self.cid))
        # Existing pricing truncates fractional cents: 6.941 * $0.35 = $2.42935.
        self.assertEqual(capture.call_args.kwargs["amount_to_capture"], 242)
        overage.assert_not_called()
        self.assertAlmostEqual(self.checkout.transaction_kwh, 6.941)

    def test_repeated_recovery_and_meter_updates_preserve_energy_baseline(self):
        start = datetime(2026, 9, 21, 20, 35, 11, tzinfo=timezone.utc)
        self.checkout.transaction_start_time = start
        self.checkout.transaction_kwh = 0
        self.checkout.transaction_last_meter_reading = 328.828
        ocpp = CitrineOSIntegration.__new__(CitrineOSIntegration)
        for seconds, total, reading in ((30, "0.009", 328868), (60, "0.050", 328900)):
            timestamp = start + timedelta(seconds=seconds)
            self.reconcile_energy("328.828", total, timestamp)
            self.reconcile_energy("328.828", total, timestamp)
            ocpp.update_checkout_with_meter_values(
                TransactionEventRequest(
                    eventType="Updated",
                    timestamp=timestamp,
                    triggerReason="MeterValuePeriodic",
                    transactionInfo={"transactionId": "30"},
                    meterValue=ocpp._ocpp16_energy_meter_value(reading),
                ),
                self.checkout,
            )
        self.assertAlmostEqual(self.checkout.transaction_kwh, 0.072)
        self.assertAlmostEqual(self.checkout.transaction_last_meter_reading, 328.900)
        # A lagging core snapshot must not rewind newer payment meter values.
        self.reconcile_energy("328.828", "0.050", start + timedelta(seconds=60))
        self.assertAlmostEqual(self.checkout.transaction_kwh, 0.072)
        self.assertAlmostEqual(self.checkout.transaction_last_meter_reading, 328.900)

    def test_snapshot_account_survives_catalog_reassignment(self):
        self.checkout.stripe_account_id = "acct_original"
        self.db.commit()
        with (
            patch(
                "stripe.PaymentIntent.retrieve",
                return_value=SimpleNamespace(status="requires_capture"),
            ),
            patch(
                "stripe.PaymentIntent.cancel",
                return_value=SimpleNamespace(status="canceled"),
            ) as cancel,
        ):
            cancel_checkout(self.db, self.cid, "driver_canceled")
        self.assertEqual(cancel.call_args.kwargs["stripe_account"], "acct_original")

    def test_pricing_returns_database_connection_without_gc(self):
        isolated = create_engine(
            "sqlite://",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.05,
        )
        Base.metadata.create_all(isolated)
        factory = sessionmaker(bind=isolated)
        with factory() as db:
            evse = seed(db)
            ck = Checkout(
                connector_id=evse.connectors[0].id,
                tariff_id=evse.connectors[0].tariff_id,
            )
            db.add(ck)
            db.commit()
            cid = ck.id
        gc.disable()
        try:
            with patch("utils.utils.get_db", side_effect=lambda: iter([factory()])):
                for _ in range(5):
                    generate_pricing(cid)
                    self.assertEqual(isolated.pool.checkedout(), 0)
        finally:
            gc.enable()
            gc.collect()
            isolated.dispose()


if __name__ == "__main__":
    unittest.main()
