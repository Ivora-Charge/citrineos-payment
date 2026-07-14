#!/usr/bin/env python3
"""Sinexcel OCPP 1.6 compatibility test.

Sends CSMS->charger requests through the CitrineOS REST API and verifies the
charger's actual OCPP replies by polling the OCPPMessages log (state=2 rows =
CallResults from the charger). Non-destructive: the QR keys are written with
the EVSEs' real checkout URLs, so the test doubles as provisioning.
"""

import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://localhost:8080/ocpp/1.6"
STATION = "sinexcel"
TENANT = 1
CHECKOUT = "https://payment-test.ivoracharge.com/checkout"

RESULTS = []


def psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "citrineos-core-ocpp-db-1", "psql", "-U", "citrine",
         "-d", "citrine", "-A", "-t", "-c", sql],
        capture_output=True, text=True, timeout=20,
    )
    return out.stdout.strip()


def max_msg_id() -> int:
    v = psql(f"SELECT COALESCE(MAX(id),0) FROM \"OCPPMessages\" "
             f"WHERE \"ocppConnectionName\"='{STATION}'")
    return int(v or 0)


def wait_reply(action: str, after_id: int, state: str = "2", timeout_s: int = 25):
    """Poll OCPPMessages for the charger's reply to a CSMS call (state=2), or
    a charger-initiated request (state=1) newer than after_id."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = psql(
            f"SELECT message FROM \"OCPPMessages\" "
            f"WHERE \"ocppConnectionName\"='{STATION}' AND action='{action}' "
            f"AND state='{state}' AND origin='cs' AND id > {after_id} "
            f"ORDER BY id DESC LIMIT 1")
        if row:
            frame = json.loads(row)
            # CallResult frame: [3, corrId, payload]; Call frame: [2, corrId, action, payload]
            return frame[-1]
        time.sleep(1.0)
    return None


def send(path: str, payload: dict):
    url = f"{BASE}/{path}?identifier={STATION}&tenantId={TENANT}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def check(name: str, ok: bool, detail: str):
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def call_and_reply(name: str, path: str, payload: dict, action: str):
    print(f"\n== {name} ==")
    marker = max_msg_id()
    confirmation = send(path, payload)
    print(f"  dispatch confirmation: {json.dumps(confirmation)[:200]}")
    reply = wait_reply(action, marker)
    if reply is None:
        check(name, False, "no reply from charger within 25s")
    return reply


def main():
    # 1. GetConfiguration (all keys): inventory what the firmware exposes.
    reply = call_and_reply("GetConfiguration (all)", "configuration/getConfiguration",
                           {}, "GetConfiguration")
    qr_keys = {}
    if reply is not None:
        keys = reply.get("configurationKey", [])
        unknown = reply.get("unknownKey", [])
        qr_keys = {k["key"]: k for k in keys if "QRCode" in k.get("key", "")}
        check("GetConfiguration (all)", bool(keys),
              f"{len(keys)} keys reported, {len(unknown)} unknown; "
              f"QR keys: {sorted(qr_keys)}")
        for k in sorted(qr_keys):
            ro = qr_keys[k].get("readonly")
            check(f"{k} writable", ro in (False, "false"),
                  f"readonly={ro} value={qr_keys[k].get('value', '')[:60]!r}")

    # 2. ChangeConfiguration: set both QR keys to the real checkout URLs.
    for n in (1, 2):
        reply = call_and_reply(
            f"ChangeConfiguration ChargePointQRCode_{n}",
            "configuration/changeConfiguration",
            {"key": f"ChargePointQRCode_{n}", "value": f"{CHECKOUT}/{STATION}-{n}"},
            "ChangeConfiguration")
        if reply is not None:
            check(f"ChangeConfiguration ChargePointQRCode_{n}",
                  reply.get("status") == "Accepted", f"status={reply.get('status')}")

    # 3. Readback: GetConfiguration for the two QR keys.
    reply = call_and_reply("GetConfiguration readback", "configuration/getConfiguration",
                           {"key": ["ChargePointQRCode_1", "ChargePointQRCode_2"]},
                           "GetConfiguration")
    if reply is not None:
        got = {k["key"]: k.get("value") for k in reply.get("configurationKey", [])}
        for n in (1, 2):
            key = f"ChargePointQRCode_{n}"
            want = f"{CHECKOUT}/{STATION}-{n}"
            check(f"readback {key}", got.get(key) == want,
                  f"value={got.get(key)!r}")

    # 4. TriggerMessage StatusNotification: exercises the charger-initiated
    #    path AND the payment service's standing-QR re-push via the new adapter.
    marker = max_msg_id()
    reply = call_and_reply("TriggerMessage StatusNotification",
                           "configuration/triggerMessage",
                           {"requestedMessage": "StatusNotification", "connectorId": 1},
                           "TriggerMessage")
    if reply is not None:
        check("TriggerMessage StatusNotification",
              reply.get("status") == "Accepted", f"status={reply.get('status')}")
        sn = wait_reply("StatusNotification", marker, state="1")
        check("StatusNotification received", sn is not None,
              f"payload={json.dumps(sn)[:120] if sn else 'none'}")

    # 5. DataTransfer with an unknown vendorId: graceful rejection expected
    #    (UnknownVendorId per spec; Rejected also acceptable).
    reply = call_and_reply("DataTransfer unknown vendor", "configuration/dataTransfer",
                           {"vendorId": "compat-test-probe"}, "DataTransfer")
    if reply is not None:
        check("DataTransfer graceful handling",
              reply.get("status") in ("UnknownVendorId", "Rejected"),
              f"status={reply.get('status')}")

    print("\n===== SUMMARY =====")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
