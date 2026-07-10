import os
import unittest

os.environ.setdefault("CONFIG_PATH", ".env.test")

from integrations.citrineos.citrineos import CitrineOSIntegration
from schemas.status_notification import StatusNotificationRequest


class NormalizeOcpp16StatusNotificationTests(unittest.TestCase):
    """The 1.6 StatusNotification shape ({connectorId, status, errorCode})
    must be normalized into the 2.0.1 request shape so the standing-QR
    logic sees status transitions (it used to be dropped at validation,
    so the QR was never re-armed after a 1.6 session ended)."""

    def setUp(self):
        self.integration = CitrineOSIntegration.__new__(CitrineOSIntegration)

    def normalize(self, payload):
        return self.integration._normalize_ocpp16_status_notification(payload)

    def test_available_maps_through_and_connector_becomes_evse(self):
        result = self.normalize(
            {"connectorId": 2, "errorCode": "NoError", "status": "Available"}
        )
        self.assertEqual(result["evseId"], 2)
        self.assertEqual(result["connectorId"], 1)
        self.assertEqual(result["connectorStatus"], "Available")

    def test_preparing_is_occupied_so_standing_qr_stays_up(self):
        result = self.normalize(
            {"connectorId": 1, "errorCode": "NoError", "status": "Preparing"}
        )
        self.assertEqual(result["connectorStatus"], "Occupied")

    def test_in_session_states_map_outside_the_qr_states(self):
        # Charging/Suspended*/Finishing must NOT map to Available/Occupied:
        # display_message_id is None right after the session-start clear, and
        # a qr-state status with a None marker re-pushes the standing QR
        # mid-session.
        for status in ("Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"):
            with self.subTest(status=status):
                result = self.normalize(
                    {"connectorId": 1, "errorCode": "NoError", "status": status}
                )
                self.assertEqual(result["connectorStatus"], "Unavailable")

    def test_terminal_states_pass_through(self):
        for status in ("Reserved", "Unavailable", "Faulted"):
            with self.subTest(status=status):
                result = self.normalize(
                    {"connectorId": 1, "errorCode": "NoError", "status": status}
                )
                self.assertEqual(result["connectorStatus"], status)

    def test_missing_timestamp_defaults_and_result_validates(self):
        result = self.normalize(
            {"connectorId": 1, "errorCode": "NoError", "status": "Available"}
        )
        parsed = StatusNotificationRequest(**result)
        self.assertIsNotNone(parsed.timestamp)

    def test_charger_timestamp_is_kept(self):
        result = self.normalize(
            {
                "connectorId": 1,
                "status": "Available",
                "timestamp": "2026-07-08T20:02:31Z",
            }
        )
        parsed = StatusNotificationRequest(**result)
        self.assertEqual(parsed.timestamp.isoformat(), "2026-07-08T20:02:31+00:00")

    def test_unknown_status_or_missing_connector_is_skipped(self):
        self.assertIsNone(
            self.normalize({"connectorId": 1, "status": "NotARealStatus"})
        )
        self.assertIsNone(self.normalize({"status": "Available"}))

    def test_station_wide_connector_zero_is_skipped(self):
        # 1.6 connectorId 0 = whole charge point; there is no EVSE to act on.
        self.assertIsNone(
            self.normalize(
                {"connectorId": 0, "errorCode": "NoError", "status": "Available"}
            )
        )
