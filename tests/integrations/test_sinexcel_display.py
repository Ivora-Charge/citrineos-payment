import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("CONFIG_PATH", ".env.test")

from integrations.charger_display import (
    SHOWN_SENTINEL,
    SinexcelDisplayAdapter,
    display_adapter_for_vendor,
    get_display_adapter,
)
from integrations.citrineos.citrineos import CitrineOSIntegration


class FakeOcpp:
    def __init__(self):
        self.sent = []

    def send_citrineos_message(self, *, station_id, tenant_id, url_path, json_payload):
        self.sent.append(
            {
                "station_id": station_id,
                "tenant_id": tenant_id,
                "url_path": url_path,
                "json_payload": json_payload,
            }
        )


class FakeDb:
    def add(self, obj):
        pass

    def commit(self):
        pass


def make_evse(ocpp_evse_id=1, display_message_id=None):
    return SimpleNamespace(
        station_id="SNX001",
        tenant_id="1",
        evse_id=f"SNX001-{ocpp_evse_id}",
        ocpp_evse_id=ocpp_evse_id,
        display_message_id=display_message_id,
        display_adapter_type="sinexcel",
    )


class SinexcelVendorMappingTests(unittest.TestCase):
    """Sinexcel's QR key is a 1.6 ChangeConfiguration ("How to Set QR-Code by
    OCPP Command", 06-2025). The adapter must only be selected for 1.6
    stations; 2.0.1+ has no ChangeConfiguration and falls back to standard."""

    def test_sinexcel_16_maps_to_sinexcel_adapter(self):
        self.assertEqual(
            display_adapter_for_vendor("Sinexcel", "ocpp1.6"), "sinexcel"
        )

    def test_sinexcel_case_insensitive(self):
        self.assertEqual(
            display_adapter_for_vendor("SINEXCEL ", "ocpp1.6"), "sinexcel"
        )

    def test_sinexcel_201_falls_back_to_standard(self):
        self.assertEqual(
            display_adapter_for_vendor("Sinexcel", "ocpp2.0.1"), "standard"
        )

    def test_sinexcel_unknown_protocol_keeps_sinexcel(self):
        # Protocol is NULL until the first reconnect of a never-seen charger;
        # keep the vendor mapping rather than guessing standard.
        self.assertEqual(display_adapter_for_vendor("Sinexcel", None), "sinexcel")

    def test_registry_resolves_sinexcel(self):
        adapter = get_display_adapter(make_evse())
        self.assertIsInstance(adapter, SinexcelDisplayAdapter)


class SinexcelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SinexcelDisplayAdapter()
        self.ocpp = FakeOcpp()
        self.db = FakeDb()

    def test_show_payment_qr_sets_per_connector_key(self):
        evse = make_evse(ocpp_evse_id=2)
        asyncio.run(
            self.adapter.show_payment_qr(
                self.ocpp,
                self.db,
                evse,
                payment_url="https://pay.example/checkout/SNX001-2",
                price=0.42,
                currency="USD",
            )
        )
        self.assertEqual(len(self.ocpp.sent), 1)
        sent = self.ocpp.sent[0]
        self.assertEqual(sent["url_path"], "configuration/changeConfiguration")
        self.assertEqual(
            sent["json_payload"],
            {
                "key": "ChargePointQRCode_2",
                "value": "https://pay.example/checkout/SNX001-2",
            },
        )
        self.assertEqual(evse.display_message_id, SHOWN_SENTINEL)

    def test_clear_payment_qr_writes_empty_value(self):
        evse = make_evse(display_message_id=SHOWN_SENTINEL)
        asyncio.run(self.adapter.clear_payment_qr(self.ocpp, self.db, evse))
        self.assertEqual(
            self.ocpp.sent[0]["json_payload"],
            {"key": "ChargePointQRCode_1", "value": ""},
        )
        self.assertIsNone(evse.display_message_id)

    def test_clear_payment_qr_noops_when_nothing_shown(self):
        evse = make_evse(display_message_id=None)
        asyncio.run(self.adapter.clear_payment_qr(self.ocpp, self.db, evse))
        self.assertEqual(self.ocpp.sent, [])

    def test_transaction_qr_uses_same_key_and_no_message_id(self):
        evse = make_evse()
        message_id = asyncio.run(
            self.adapter.show_transaction_qr(
                self.ocpp,
                self.db,
                evse,
                payment_url="https://pay.example/checkout/SNX001-1?pay=x",
                transaction_id="7",
                price=0.42,
                currency="USD",
            )
        )
        self.assertIsNone(message_id)
        self.assertEqual(
            self.ocpp.sent[0]["json_payload"]["key"], "ChargePointQRCode_1"
        )


class ChangeConfigurationTranslationTests(unittest.TestCase):
    """For 1.6 stations every outgoing call passes _translate_call_ocpp16;
    changeConfiguration is 1.6-native and must pass through, not be dropped
    as 'no 1.6 equivalent'."""

    def setUp(self):
        self.integration = CitrineOSIntegration.__new__(CitrineOSIntegration)

    def test_change_configuration_passes_through(self):
        payload = {"key": "ChargePointQRCode_1", "value": "https://x"}
        result = self.integration._translate_call_ocpp16(
            "configuration/changeConfiguration", payload
        )
        self.assertEqual(
            result, ("configuration/changeConfiguration", payload)
        )


if __name__ == "__main__":
    unittest.main()
