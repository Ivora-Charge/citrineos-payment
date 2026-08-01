"""RCD vendor-protocol sync (integrations/rcd_vendor.py).

Covers the rate push (DefaultPrice + best-effort rate_req) and the
charger-initiated realtime_status / bill consumption. The billing-correctness
invariant pinned here: RCD's session-relative kWh figures must NEVER reach the
checkout's register-delta energy accumulator -- only SoC rides the session
pipeline.
"""

import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config
from db.init_db import Base, Checkout, Connector, Evse, Tariff
from integrations import rcd_vendor
from integrations.citrineos.citrineos import CitrineOSIntegration
from schemas.transaction_event import MeasurandEnumType

STATION = "R132739260301001"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class RcdVendorTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.tariff = Tariff(
            id=1,
            price_kwh=0.30,
            price_minute=0.02,
            price_session=1.00,
            currency="usd",
            tax_rate=0.0,
            authorization_amount=20.0,
            payment_fee=0.0,
        )
        self.evse = Evse(
            id=1,
            evse_id=f"{STATION}-1",
            ocpp_evse_id=1,
            status="Available",
            station_id=STATION,
            tenant_id="3",
            display_adapter_type="renova",
        )
        self.connector = Connector(
            id=1,
            connector_id=f"{STATION}-1-1",
            power_type="AC_1_PHASE",
            max_voltage=240,
            max_amperage=32,
            evse_id=1,
            tariff_id=1,
        )
        self.db.add_all([self.tariff, self.evse, self.connector])
        self.db.commit()
        self.db.refresh(self.evse)

        self.integration = CitrineOSIntegration.__new__(CitrineOSIntegration)
        self.integration.send_citrineos_message = MagicMock(return_value=None)
        self.integration._station_vendor = MagicMock(
            return_value=("RCD", "ocpp1.6")
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def sent(self, url_path):
        return [
            call.kwargs
            for call in self.integration.send_citrineos_message.call_args_list
            if call.kwargs.get("url_path") == url_path
        ]


class RatePayloadTests(RcdVendorTestBase):
    def test_default_price_matches_observed_charger_schema(self):
        value = json.loads(rcd_vendor.build_default_price_value(self.tariff))
        self.assertEqual(
            value["chargingPrice"],
            {"flatFee": 1.00, "hourPrice": 1.20, "kWhPrice": 0.30},
        )
        self.assertIn("0.30 USD/kWh", value["priceText"])
        self.assertIn("1.20 USD/h", value["priceText"])
        self.assertIn("1.00 USD session fee", value["priceText"])
        self.assertEqual(value["priceTextOffline"], rcd_vendor.OFFLINE_PRICE_TEXT)

    def test_price_text_lines_capped_at_50_chars(self):
        # All four components active -> text must wrap instead of overflowing
        # the charger screen (~50 chars/line, observed live).
        self.tariff.payment_fee = 0.50
        value = json.loads(rcd_vendor.build_default_price_value(self.tariff))
        lines = value["priceText"].split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), rcd_vendor.MAX_PRICE_TEXT_LINE)
        # Nothing lost in the wrap.
        flat = ", ".join(lines).replace("\n", "")
        for fragment in (
            "0.30 USD/kWh",
            "time of use",
            "session fee",
            "transaction fee",
        ):
            self.assertIn(fragment, flat)

    def test_transaction_fee_shown_in_text_but_not_in_charging_price(self):
        self.tariff.payment_fee = 0.50
        value = json.loads(rcd_vendor.build_default_price_value(self.tariff))
        self.assertIn("+0.50 USD transaction fee", value["priceText"])
        # The fee is checkout-applied; it must not leak into the charger's
        # own offline billing math.
        self.assertNotIn("0.5", json.dumps(value["chargingPrice"]))

    def test_default_price_zero_tariff_has_fallback_text(self):
        bare = Tariff(
            currency="usd", tax_rate=0.0, authorization_amount=0.0, payment_fee=0.0
        )
        value = json.loads(rcd_vendor.build_default_price_value(bare))
        self.assertEqual(value["priceText"], "0.00 USD/kWh")

    def test_rate_req_uses_qrcode_dialect(self):
        data = json.loads(rcd_vendor.build_rate_req_data(self.evse, self.tariff))
        self.assertEqual(data["connector_id"], 1)
        self.assertEqual(data["evse_id"], f"{STATION}-1")
        self.assertEqual(data["kwh_price"], 0.30)
        self.assertEqual(data["unit"], "USD/kWh")


class PushRatesTests(RcdVendorTestBase):
    def test_pushes_default_price_and_rate_req_on_16(self):
        run(rcd_vendor.push_rates(self.integration, self.db, self.evse))

        config_calls = self.sent("configuration/changeConfiguration")
        self.assertEqual(len(config_calls), 1)
        self.assertEqual(config_calls[0]["json_payload"]["key"], "DefaultPrice")
        self.assertIn("kWhPrice", config_calls[0]["json_payload"]["value"])

        dt_calls = self.sent("configuration/dataTransfer")
        self.assertEqual(len(dt_calls), 1)
        self.assertEqual(dt_calls[0]["json_payload"]["vendorId"], "rcd")
        self.assertEqual(dt_calls[0]["json_payload"]["messageId"], "rate_req")

    def test_default_price_skipped_on_ocpp2_but_rate_req_still_sent(self):
        self.integration._station_vendor.return_value = ("RCD", "ocpp2.1")
        run(rcd_vendor.push_rates(self.integration, self.db, self.evse))
        self.assertEqual(self.sent("configuration/changeConfiguration"), [])
        self.assertEqual(len(self.sent("configuration/dataTransfer")), 1)

    def test_non_rcd_evse_is_ignored(self):
        self.evse.display_adapter_type = "standard"
        run(rcd_vendor.push_rates(self.integration, self.db, self.evse))
        self.integration.send_citrineos_message.assert_not_called()

    def test_no_tariff_is_a_noop(self):
        self.connector.tariff_id = None
        self.db.commit()
        self.db.refresh(self.evse)
        run(rcd_vendor.push_rates(self.integration, self.db, self.evse))
        self.integration.send_citrineos_message.assert_not_called()

    def test_send_failure_never_raises(self):
        self.integration.send_citrineos_message.side_effect = RuntimeError("boom")
        run(rcd_vendor.push_rates(self.integration, self.db, self.evse))


class ZoneOffsetTests(RcdVendorTestBase):
    def test_offset_string_matches_firmware_format(self):
        # Etc/GMT+7 is UTC-7 (POSIX sign inversion); firmware wants unpadded
        # hours + 2-digit minutes.
        self.assertEqual(rcd_vendor._zone_offset_string("Etc/GMT+7"), "UTC-7:00")
        self.assertEqual(rcd_vendor._zone_offset_string("Etc/GMT-8"), "UTC+8:00")
        self.assertEqual(
            rcd_vendor._zone_offset_string("Asia/Kolkata"), "UTC+5:30"
        )
        self.assertIsNone(rcd_vendor._zone_offset_string("Not/AZone"))

    def test_push_sends_zone_offset_data_transfer(self):
        with patch.object(Config, "CHARGER_DISPLAY_TIMEZONE", "Etc/GMT+7"):
            run(rcd_vendor.push_zone_offset(self.integration, self.db, self.evse))
        calls = self.sent("configuration/dataTransfer")
        self.assertEqual(len(calls), 1)
        payload = calls[0]["json_payload"]
        self.assertEqual(payload["messageId"], "zone_offset_req")
        self.assertEqual(
            json.loads(payload["data"]), {"zoneOffset": "UTC-7:00"}
        )

    def test_push_skipped_without_timezone_or_for_other_vendors(self):
        with patch.object(Config, "CHARGER_DISPLAY_TIMEZONE", ""):
            run(rcd_vendor.push_zone_offset(self.integration, self.db, self.evse))
        self.evse.display_adapter_type = "standard"
        with patch.object(Config, "CHARGER_DISPLAY_TIMEZONE", "Etc/GMT+7"):
            run(rcd_vendor.push_zone_offset(self.integration, self.db, self.evse))
        self.integration.send_citrineos_message.assert_not_called()


class HandleDataTransferTests(RcdVendorTestBase):
    def handle(self, payload):
        run(
            rcd_vendor.handle_data_transfer(
                self.integration, payload=payload, station_id=STATION
            )
        )

    def test_other_vendor_is_ignored(self):
        self.integration.process_transaction_updated = MagicMock()
        self.handle({"vendorId": "acme", "messageId": "realtime_status_req"})
        self.integration.process_transaction_updated.assert_not_called()

    def test_realtime_status_feeds_only_soc_into_session_pipeline(self):
        captured = {}

        async def fake_update(transaction_event, station_id):
            captured["event"] = transaction_event
            captured["station_id"] = station_id

        self.integration.process_transaction_updated = fake_update
        self.handle(
            {
                "vendorId": "rcd",
                "messageId": "realtime_status_req",
                "data": json.dumps(
                    {
                        "connector": 1,
                        "serial": 42,
                        "electricEnergy": 3.210,
                        "amount": 0.96,
                        "soc": 55,
                        "chargeTime": 600,
                    }
                ),
            }
        )

        self.assertEqual(captured["station_id"], STATION)
        event = captured["event"]
        self.assertEqual(event.transactionInfo.transactionId, "42")
        measurands = [
            sv.measurand
            for mv in event.meterValue
            for sv in mv.sampledValue
        ]
        self.assertEqual(measurands, [MeasurandEnumType.SoC])

    def test_realtime_status_without_transaction_is_dropped(self):
        self.integration.process_transaction_updated = MagicMock()
        self.handle(
            {
                "vendorId": "rcd",
                "messageId": "realtime_status_req",
                "data": json.dumps({"connector": 1, "serial": 0, "soc": 50}),
            }
        )
        self.integration.process_transaction_updated.assert_not_called()

    def test_malformed_data_never_raises(self):
        self.handle(
            {"vendorId": "rcd", "messageId": "realtime_status_req", "data": "]["}
        )
        self.handle({"vendorId": "rcd", "messageId": "bill_req", "data": None})

    def test_bill_energy_drift_warns(self):
        checkout = Checkout(
            connector_id=1,
            remote_request_transaction_id="42",
            transaction_kwh=3.0,
        )
        self.db.add(checkout)
        self.db.commit()

        bill = {
            "vendorId": "rcd",
            "messageId": "bill_req",
            "data": json.dumps(
                {"connector": 1, "serial": 42, "electricEnergy": 4.5, "amount": 1.35}
            ),
        }
        with patch(
            "integrations.rcd_vendor.get_db", side_effect=lambda: iter([self.db])
        ):
            with self.assertLogs(level="WARNING") as logs:
                self.handle(bill)
        self.assertTrue(any("drift" in line for line in logs.output))

    def test_bill_matching_energy_is_quiet(self):
        checkout = Checkout(
            connector_id=1,
            remote_request_transaction_id="42",
            transaction_kwh=4.49,
        )
        self.db.add(checkout)
        self.db.commit()

        bill = {
            "vendorId": "rcd",
            "messageId": "bill_req",
            "data": json.dumps({"connector": 1, "serial": 42, "electricEnergy": 4.5}),
        }
        with patch(
            "integrations.rcd_vendor.get_db", side_effect=lambda: iter([self.db])
        ):
            with self.assertNoLogs(level="WARNING"):
                self.handle(bill)


if __name__ == "__main__":
    unittest.main()
