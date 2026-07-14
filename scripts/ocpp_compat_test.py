#!/usr/bin/env python3
"""Full OCPP compatibility test for a charger connected to this platform.

Drives the charger through the CitrineOS REST API and verifies its actual
OCPP replies from the OCPPMessages log, then checks the payment-platform
integration (catalog EVSEs, tariffs, display adapter, QR delivery).

Usage (on the CSMS box):
    python3 scripts/ocpp_compat_test.py <station_id> [--tenant N] [--allow-reset]

Non-destructive by default:
  - ChangeConfiguration re-writes a key's CURRENT value (round-trip proof,
    no config change).
  - No remote start/stop, no reset unless --allow-reset (soft reset +
    reconnect check).
Exit code 0 = all checks passed (SKIP/WARN allowed), 1 = at least one FAIL.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError, URLError

CITRINE = "http://localhost:8080"
PAYMENT = "http://localhost:9010"
DB_CONTAINER = "citrineos-core-ocpp-db-1"

RESULTS = []  # (level, name, detail); level in PASS/FAIL/WARN/SKIP


def record(level, name, detail=""):
    RESULTS.append((level, name, detail))
    print(f"  {level:<4}  {name}" + (f": {detail}" if detail else ""))


def psql(sql):
    out = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", "citrine", "-d", "citrine",
         "-A", "-t", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()}")
    return out.stdout.strip()


class Charger:
    def __init__(self, station, tenant):
        self.station = station
        self.tenant = tenant
        row = psql(
            "SELECT protocol, \"isOnline\", \"chargePointVendor\", \"chargePointModel\", "
            "\"firmwareVersion\", id FROM \"ChargingStations\" "
            f"WHERE \"ocppConnectionName\"='{station}' AND \"tenantId\"={tenant}")
        if not row:
            raise SystemExit(f"Station {station!r} not found for tenant {tenant}")
        (self.protocol, online, self.vendor, self.model,
         self.firmware, self.db_id) = row.split("\t")
        self.online = online == "t"
        self.ocpp16 = (self.protocol or "").startswith("ocpp1.6")
        self.base = f"{CITRINE}/ocpp/1.6" if self.ocpp16 else f"{CITRINE}/ocpp/2.0.1"

    # ---- transport ------------------------------------------------------
    def send(self, path, payload):
        url = f"{self.base}/{path}?identifier={self.station}&tenantId={self.tenant}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def msg_marker(self):
        v = psql("SELECT COALESCE(MAX(id),0) FROM \"OCPPMessages\" "
                 f"WHERE \"ocppConnectionName\"='{self.station}'")
        return int(v or 0)

    def wait_frame(self, action, after_id, state="2", timeout_s=20):
        """Charger reply to a CSMS call (state=2) or charger-initiated
        request (state=1), newer than after_id. Returns the frame payload."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            row = psql(
                "SELECT message FROM \"OCPPMessages\" "
                f"WHERE \"ocppConnectionName\"='{self.station}' AND action='{action}' "
                f"AND state='{state}' AND origin='cs' AND id > {after_id} "
                "ORDER BY id DESC LIMIT 1")
            if row:
                return json.loads(row)[-1]
            time.sleep(1.0)
        return None

    def call(self, name, path, payload, action, timeout_s=20):
        """Send a CSMS call; FAIL-record and return None if no charger reply."""
        marker = self.msg_marker()
        try:
            self.send(path, payload)
        except (HTTPError, URLError) as e:
            record("FAIL", name, f"REST dispatch failed: {e}")
            return None
        reply = self.wait_frame(action, marker, timeout_s=timeout_s)
        if reply is None:
            record("FAIL", name, f"no {action} reply within {timeout_s}s")
        return reply


def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, None
    except URLError:
        return None, None


# ---- 1.6 test sections ---------------------------------------------------

def test_configuration_16(cp):
    print("\n== Configuration ==")
    reply = cp.call("GetConfiguration (all)", "configuration/getConfiguration",
                    {}, "GetConfiguration")
    keys = {}
    if reply is not None:
        keys = {k["key"]: k for k in reply.get("configurationKey", [])}
        record("PASS", "GetConfiguration (all)", f"{len(keys)} keys reported")
        for core_key in ("HeartbeatInterval", "NumberOfConnectors",
                         "SupportedFeatureProfiles", "MeterValueSampleInterval"):
            if core_key in keys:
                record("PASS", f"core key {core_key}",
                       f"value={keys[core_key].get('value')!r}")
            else:
                record("WARN", f"core key {core_key}", "not reported")

    # Round-trip: rewrite HeartbeatInterval with its CURRENT value.
    hb = keys.get("HeartbeatInterval", {})
    if hb.get("value") is not None and hb.get("readonly") in (False, "false"):
        reply = cp.call("ChangeConfiguration round-trip",
                        "configuration/changeConfiguration",
                        {"key": "HeartbeatInterval", "value": hb["value"]},
                        "ChangeConfiguration")
        if reply is not None:
            status = reply.get("status")
            record("PASS" if status in ("Accepted", "RebootRequired") else "FAIL",
                   "ChangeConfiguration round-trip", f"status={status}")
        reply = cp.call("GetConfiguration readback", "configuration/getConfiguration",
                        {"key": ["HeartbeatInterval"]}, "GetConfiguration")
        if reply is not None:
            got = {k["key"]: k.get("value")
                   for k in reply.get("configurationKey", [])}
            record("PASS" if got.get("HeartbeatInterval") == hb["value"] else "FAIL",
                   "readback HeartbeatInterval", f"value={got.get('HeartbeatInterval')!r}")
    else:
        record("SKIP", "ChangeConfiguration round-trip",
               "HeartbeatInterval missing or readonly")
    return keys


def test_triggers_16(cp, connector_ids):
    print("\n== TriggerMessage / charger-initiated events ==")
    for requested, extra in [("Heartbeat", {}), ("MeterValues", {"connectorId": 1})]:
        marker = cp.msg_marker()
        reply = cp.call(f"TriggerMessage {requested}", "configuration/triggerMessage",
                        {"requestedMessage": requested, **extra}, "TriggerMessage")
        if reply is None:
            continue
        status = reply.get("status")
        if status != "Accepted":
            # MeterValues trigger is optional in 1.6 -- NotImplemented is OK.
            record("WARN" if requested == "MeterValues" else "FAIL",
                   f"TriggerMessage {requested}", f"status={status}")
            continue
        record("PASS", f"TriggerMessage {requested}", "status=Accepted")
        frame = cp.wait_frame(requested, marker, state="1")
        record("PASS" if frame is not None else "FAIL",
               f"{requested} received", "" if frame is None
               else json.dumps(frame)[:100])

    for cid in connector_ids:
        marker = cp.msg_marker()
        reply = cp.call(f"TriggerMessage StatusNotification c{cid}",
                        "configuration/triggerMessage",
                        {"requestedMessage": "StatusNotification", "connectorId": cid},
                        "TriggerMessage")
        if reply is None:
            continue
        if reply.get("status") != "Accepted":
            record("FAIL", f"TriggerMessage StatusNotification c{cid}",
                   f"status={reply.get('status')}")
            continue
        frame = cp.wait_frame("StatusNotification", marker, state="1")
        record("PASS" if frame is not None else "FAIL",
               f"StatusNotification c{cid}",
               f"status={frame.get('status')}" if frame else "not received")


def test_misc_16(cp):
    print("\n== Availability / cache / vendor handling ==")
    reply = cp.call("ChangeAvailability Operative", "configuration/changeAvailability",
                    {"connectorId": 0, "type": "Operative"}, "ChangeAvailability")
    if reply is not None:
        record("PASS" if reply.get("status") in ("Accepted", "Scheduled") else "FAIL",
               "ChangeAvailability Operative", f"status={reply.get('status')}")

    reply = cp.call("ClearCache", "evdriver/clearCache", {}, "ClearCache")
    if reply is not None:
        # Rejected is spec-legal (e.g. no cache support) -- informational.
        record("PASS" if reply.get("status") == "Accepted" else "WARN",
               "ClearCache", f"status={reply.get('status')}")

    reply = cp.call("DataTransfer unknown vendor", "configuration/dataTransfer",
                    {"vendorId": "compat-test-probe"}, "DataTransfer")
    if reply is not None:
        record("PASS" if reply.get("status") in ("UnknownVendorId", "Rejected")
               else "FAIL",
               "DataTransfer graceful handling", f"status={reply.get('status')}")


def test_reset(cp):
    print("\n== Reset / reconnect (--allow-reset) ==")
    marker = cp.msg_marker()
    reply = cp.call("Reset Soft", "configuration/reset", {"type": "Soft"}, "Reset")
    if reply is None:
        return
    if reply.get("status") != "Accepted":
        record("FAIL", "Reset Soft", f"status={reply.get('status')}")
        return
    record("PASS", "Reset Soft", "status=Accepted")
    boot = cp.wait_frame("BootNotification", marker, state="1", timeout_s=180)
    record("PASS" if boot is not None else "FAIL", "reboot + BootNotification",
           json.dumps(boot)[:100] if boot else "no boot within 180s")


# ---- 2.0.1 subset ---------------------------------------------------------

def test_ocpp201_subset(cp, connector_ids):
    print("\n== OCPP 2.0.1 subset ==")
    reply = cp.call("GetVariables TxCtrlr.TxStartPoint", "monitoring/getVariables",
                    {"getVariableData": [{"component": {"name": "TxCtrlr"},
                                          "variable": {"name": "TxStartPoint"}}]},
                    "GetVariables")
    if reply is not None:
        result = (reply.get("getVariableResult") or [{}])[0]
        record("PASS" if result.get("attributeStatus") == "Accepted" else "WARN",
               "GetVariables TxCtrlr.TxStartPoint",
               f"{result.get('attributeStatus')} value={result.get('attributeValue')!r}")
    for cid in connector_ids:
        marker = cp.msg_marker()
        reply = cp.call(f"TriggerMessage StatusNotification evse {cid}",
                        "configuration/triggerMessage",
                        {"requestedMessage": "StatusNotification",
                         "evse": {"id": cid}}, "TriggerMessage")
        if reply is not None and reply.get("status") == "Accepted":
            frame = cp.wait_frame("StatusNotification", marker, state="1")
            record("PASS" if frame is not None else "FAIL",
                   f"StatusNotification evse {cid}",
                   f"{frame.get('connectorStatus')}" if frame else "not received")


# ---- platform integration -------------------------------------------------

def test_platform(cp, citrine_connector_ids, config_keys):
    print("\n== Platform integration (payment/catalog) ==")
    rows = psql(
        "SELECT e.evse_id, e.ocpp_evse_id, e.display_adapter_type, c.tariff_id, "
        "e.location_id, e.status FROM payment_evses e "
        "LEFT JOIN payment_connectors c ON c.evse_id = e.id "
        f"WHERE e.station_id='{cp.station}' ORDER BY e.ocpp_evse_id")
    if not rows:
        record("FAIL", "payment catalog",
               "no payment_evses rows -- claim/onboard + catalog sync missing")
        return
    payment = [r.split("\t") for r in rows.splitlines()]
    payment_ids = sorted(int(p[1]) for p in payment)
    record("PASS", "payment catalog", f"{len(payment)} EVSE(s): "
           + ", ".join(f"{p[0]} adapter={p[2]} tariff={p[3]}" for p in payment))

    # Connector numbering: charger-reported ids vs catalog ids (catches the
    # dual-gun units that report connectorId 1 and 3).
    reported = sorted(citrine_connector_ids)
    if reported and payment_ids != reported:
        record("FAIL", "connector numbering",
               f"charger reports {reported}, payment catalog has {payment_ids}"
               " -- QR/session routing will miss the mismatched gun(s)")
    else:
        record("PASS", "connector numbering", f"{payment_ids}")

    for evse_id, _, adapter, tariff_id, _, _ in payment:
        if not tariff_id:
            record("FAIL", f"tariff on {evse_id}", "no tariff -- QR/payment refused")
        st, _ = http_get(f"{PAYMENT}/api/evses/{evse_id}")
        record("PASS" if st == 200 else "FAIL",
               f"checkout API {evse_id}", f"HTTP {st}")

    # Display adapter sanity + QR delivery evidence.
    adapter = payment[0][2] or "standard"
    if adapter == "none":
        record("WARN", "QR delivery", "no display channel -- printed QR required")
    elif adapter == "sinexcel":
        for _, ocpp_id, _, _, _, evse_status in payment:
            key = f"ChargePointQRCode_{ocpp_id}"
            val = (config_keys.get(key) or {}).get("value") or ""
            if "/checkout/" in val:
                record("PASS", f"standing QR in {key}", val[:70])
            elif evse_status not in ("Available", "Occupied"):
                # In-session / unavailable EVSEs get their QR cleared by
                # design (empty-value ChangeConfiguration).
                record("PASS", f"standing QR in {key}",
                       f"cleared while EVSE is {evse_status} (expected)")
            else:
                record("FAIL", f"standing QR in {key}", "empty on an idle EVSE")
    else:
        row = psql(
            "SELECT message FROM \"OCPPMessages\" "
            f"WHERE \"ocppConnectionName\"='{cp.station}' AND state='2' AND ("
            "action='DataTransfer' OR action='SetDisplayMessage') "
            "ORDER BY id DESC LIMIT 1")
        if row:
            payload = json.loads(row)[-1]
            record("PASS" if payload.get("status") == "Accepted" else "WARN",
                   "last QR display push", f"status={payload.get('status')}")
        else:
            record("WARN", "QR delivery", "no display push observed yet")

    op = psql(
        "SELECT o.name, o.stripe_account_id FROM payment_locations l "
        "JOIN payment_operators o ON o.id = l.operator_id "
        f"WHERE l.id = {payment[0][4]}") if payment[0][4] else ""
    if op:
        name, acct = op.split("\t")
        record("PASS" if acct else "FAIL", "operator / Stripe account",
               f"{name} -> {acct or 'MISSING'}")
    else:
        record("FAIL", "operator / Stripe account", "EVSE has no location/operator")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("station")
    ap.add_argument("--tenant", type=int, default=1)
    ap.add_argument("--allow-reset", action="store_true",
                    help="also soft-reset the charger and verify it reconnects")
    args = ap.parse_args()

    cp = Charger(args.station, args.tenant)
    print(f"Station {cp.station} (tenant {args.tenant}): {cp.vendor} {cp.model} "
          f"fw={cp.firmware} protocol={cp.protocol} online={cp.online}")
    record("PASS" if cp.online else "FAIL", "connectivity",
           "online" if cp.online else "offline -- aborting")
    if not cp.online:
        return finish()

    # Ground truth for the charger's connector numbering is what it has
    # actually sent (StatusNotification history), NOT the CSMS Connectors
    # table: 1.6 commissioning stores EvseType.databaseId (a global PK) as
    # evseTypeConnectorId, which drifts from the OCPP connector id once other
    # tenants/stations exist (seen as phantom "connector 3" on 2-gun units).
    connector_ids = [int(r) for r in psql(
        "SELECT DISTINCT (message->3->>'connectorId')::int FROM \"OCPPMessages\" "
        f"WHERE \"ocppConnectionName\"='{cp.station}' AND action='StatusNotification' "
        "AND state='1' AND (message->3->>'connectorId')::int > 0 ORDER BY 1"
    ).splitlines() if r] if cp.ocpp16 else []
    commissioned_ids = [int(r) for r in psql(
        "SELECT \"evseTypeConnectorId\" FROM \"Connectors\" "
        f"WHERE \"stationId\"={cp.db_id} ORDER BY 1").splitlines() if r]
    if not connector_ids:
        connector_ids = commissioned_ids
    record("PASS" if connector_ids else "WARN", "connectors commissioned",
           f"charger-reported connector ids: {connector_ids or 'none yet'}")
    if connector_ids and commissioned_ids and set(commissioned_ids) != set(connector_ids):
        record("WARN", "CSMS connector labels",
               f"Connectors.evseTypeConnectorId={commissioned_ids} vs charger-reported "
               f"{connector_ids} -- core 1.6 commissioning stores EvseType.databaseId; "
               "cosmetic for routing but UI/2.0.1 views show phantom ids")

    config_keys = {}
    if cp.ocpp16:
        config_keys = test_configuration_16(cp)
        test_triggers_16(cp, connector_ids or [1])
        test_misc_16(cp)
    else:
        test_ocpp201_subset(cp, connector_ids or [1])

    test_platform(cp, connector_ids, config_keys)

    if args.allow_reset:
        test_reset(cp)

    return finish()


def finish():
    print("\n===== SUMMARY =====")
    counts = {}
    for level, name, _ in RESULTS:
        counts[level] = counts.get(level, 0) + 1
        if level != "PASS":
            print(f"{level:<4}  {name}")
    total = len(RESULTS)
    print(f"\n{counts.get('PASS', 0)}/{total} passed"
          + "".join(f", {v} {k}" for k, v in counts.items() if k != "PASS"))
    sys.exit(1 if counts.get("FAIL") else 0)


if __name__ == "__main__":
    main()
