import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("CONFIG_PATH", ".env.test")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.endpoints import checkouts as checkouts_module
from schemas.checkouts import Checkout, Pricing

app = FastAPI()
app.include_router(checkouts_module.router, prefix="/api/checkouts")

client = TestClient(app)


def _snapshot(end_time=None, kwh=0.5):
    return Checkout(
        id=1,
        payment_intent_id="pi_test",
        connector_id=1,
        tariff_id=1,
        remote_request_status="Accepted",
        remote_request_transaction_id="42",
        transaction_start_time=datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc),
        transaction_end_time=end_time,
        transaction_kwh=kwh,
        power_active_import=7.2,
        transaction_soc=55.0,
        pricing=Pricing(currency="usd", tax_rate=0.0, payment_fee=0.0),
        evse_status="Occupied",
    )


def _frames(response_text):
    return [
        line[len("data: ") :]
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


class TestCheckoutEvents(unittest.TestCase):
    def test_unknown_checkout_404s(self):
        with patch.object(
            checkouts_module, "_checkout_snapshot", return_value=None
        ):
            response = client.get("/api/checkouts/999/events")
        self.assertEqual(response.status_code, 404)

    def test_stream_closes_after_final_snapshot(self):
        ended = _snapshot(end_time=datetime(2026, 8, 3, 19, 0, tzinfo=timezone.utc))
        with patch.object(
            checkouts_module, "_checkout_snapshot", return_value=ended
        ):
            response = client.get("/api/checkouts/1/events")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        frames = _frames(response.text)
        self.assertEqual(len(frames), 1)
        self.assertIn('"transaction_end_time":', frames[0])

    def test_heartbeat_refreshes_until_session_ends(self):
        # First frame: running session; after one (shortened) heartbeat the
        # next snapshot carries the end time and the stream terminates.
        running = _snapshot(end_time=None, kwh=0.1)
        ended = _snapshot(
            end_time=datetime(2026, 8, 3, 19, 0, tzinfo=timezone.utc), kwh=0.2
        )
        with (
            patch.object(
                checkouts_module,
                "_checkout_snapshot",
                side_effect=[running, running, ended],
            ),
            patch.object(checkouts_module, "EVENTS_SNAPSHOT_SECONDS", 0.05),
        ):
            response = client.get("/api/checkouts/1/events")
        frames = _frames(response.text)
        self.assertEqual(len(frames), 2)
        self.assertIn('"transaction_kwh":0.1', frames[0])
        self.assertIn('"transaction_kwh":0.2', frames[1])

    def test_vanished_checkout_ends_stream(self):
        running = _snapshot(end_time=None)
        with (
            patch.object(
                checkouts_module,
                "_checkout_snapshot",
                side_effect=[running, running, None],
            ),
            patch.object(checkouts_module, "EVENTS_SNAPSHOT_SECONDS", 0.05),
        ):
            response = client.get("/api/checkouts/1/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_frames(response.text)), 1)


if __name__ == "__main__":
    unittest.main()
