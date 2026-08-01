"""RCD ("Renova / Rainbow") vendor-protocol sync over OCPP 1.6 DataTransfer.

RCD chargers tunnel a proprietary protocol through the standard DataTransfer
action (vendorId "rcd", JSON-string payloads, snake_case keys, messageIds of
the form ``<op>_req`` answered by ``<op>_conf``; see
RCD_OCPP1.6_DataTransfer_Spec.pdf). The QR delivery half already lives in
charger_display.RenovaDisplayAdapter; this module adds the two payment-facing
halves:

* Rate sync (CS -> CP), ``push_rates``: keep the price the charger shows and
  bills locally in step with the connector tariff. Two channels:
  - ChangeConfiguration ``DefaultPrice`` -- VERIFIED on the live AC family
    ("HengTong" controller, fw V72.78: write Accepted + exact readback,
    2026-07-31). 1.6-only (it's a 1.6 config key), station-global.
  - DataTransfer ``rate_req`` -- the firmware dispatches the messageId (it
    answers ``rate_conf``) but every payload shape tried so far is refused
    with ``result:-1``. Sent best-effort and logged so a firmware that does
    accept it (e.g. the D60 DC family the spec was recovered from) reveals
    the working schema in the logs.

* In-session telemetry (CP -> CS), ``handle_data_transfer``: consume
  charger-initiated ``realtime_status`` (live SoC/energy/cost during a
  session) and ``bill`` (end-of-session billing record, spec section 4).
  SoC is fed into the normal session pipeline as a synthetic Updated event
  (checkout.transaction_soc). Energy/amount are logged for reconciliation
  but deliberately NOT fed into the checkout energy accumulator: it tracks
  the meter *register* from MeterValues, and mixing session-relative kWh
  into register deltas would corrupt billing.

Everything here is best-effort: a vendor-protocol hiccup must never break
boots, catalog syncs, or the AMQP consumer loop.
"""

import json
from datetime import datetime, timezone
from logging import debug, info, warning
from zoneinfo import ZoneInfo

from config import Config
from db.init_db import get_db
from schemas.transaction_event import (
    MeasurandEnumType,
    MeterValueType,
    SampledValueType,
    TransactionEventEnumType,
    TransactionType,
    TriggerReasonEnumType,
    TransactionEventRequest,
)

RCD_VENDOR_ID = "rcd"
RATE_MESSAGE_ID = "rate_req"
ZONE_OFFSET_MESSAGE_ID = "zone_offset_req"
OFFLINE_PRICE_TEXT = "The station is offline."

# The charger screen overflows past ~50 chars per line (observed live on the
# AC family, 2026-07-31); priceText lines are wrapped to fit.
MAX_PRICE_TEXT_LINE = 50

# Bill/realtime energy reconciliation: warn when the charger's own session
# total disagrees with ours by more than this (kWh, plus 2% relative).
BILL_ENERGY_TOLERANCE_KWH = 0.05


def is_rcd_evse(evse) -> bool:
    """True when the EVSE resolved to an RCD display adapter ("renova" /
    "renova21"). display_adapter_type is re-derived from the charger's
    BootNotification vendor on every standing-QR push, so it is fresh by the
    time rates are pushed (boot handler pushes the QR first)."""
    return (
        str(getattr(evse, "display_adapter_type", "") or "")
        .strip()
        .lower()
        .startswith("renova")
    )


def _connector_tariff(evse):
    """The first connector tariff wired to this EVSE, or None."""
    try:
        for connector in evse.connectors or []:
            if connector.tariff is not None:
                return connector.tariff
    except Exception:
        pass
    return None


def _wrap_price_text(parts: "list[str]") -> str:
    """Join tariff components into display lines, packing as many onto each
    line (comma-separated) as fit in MAX_PRICE_TEXT_LINE. A single component
    longer than the limit gets its own (overflowing) line rather than being
    split mid-word."""
    lines, current = [], ""
    for part in parts:
        candidate = f"{current}, {part}" if current else part
        if current and len(candidate) > MAX_PRICE_TEXT_LINE:
            lines.append(current)
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def build_default_price_value(tariff) -> str:
    """The ``DefaultPrice`` configuration value for a tariff, in the exact
    shape the charger itself stores (GetConfiguration readback 2026-07-31):
    {"chargingPrice":{"flatFee","hourPrice","kWhPrice"},"priceText",...}.
    hourPrice is per HOUR; our tariffs price per minute.

    ``priceText`` is free text the charger renders on-screen (the factory
    value was a time-of-use price table as text), so every non-zero tariff
    component is spelled out, including the transaction fee -- which stays
    OUT of chargingPrice: those numbers drive the charger's own offline
    billing math, and the card fee is applied by our checkout, not the meter.
    """
    kwh_price = float(tariff.price_kwh or 0.0)
    hour_price = float(tariff.price_minute or 0.0) * 60
    flat_fee = float(tariff.price_session or 0.0)
    payment_fee = float(getattr(tariff, "payment_fee", 0.0) or 0.0)
    currency = (tariff.currency or "").upper()

    parts = []
    if kwh_price:
        parts.append(f"{kwh_price:.2f} {currency}/kWh")
    if hour_price:
        parts.append(f"{hour_price:.2f} {currency}/h (time of use)")
    if flat_fee:
        parts.append(f"{flat_fee:.2f} {currency} session fee")
    if payment_fee:
        parts.append(f"+{payment_fee:.2f} {currency} transaction fee")
    price_text = _wrap_price_text(parts) or f"0.00 {currency}/kWh"

    return json.dumps(
        {
            "chargingPrice": {
                "flatFee": flat_fee,
                "hourPrice": hour_price,
                "kWhPrice": kwh_price,
            },
            "priceText": price_text,
            "priceTextOffline": OFFLINE_PRICE_TEXT,
        },
        separators=(",", ":"),
    )


def build_rate_req_data(evse, tariff) -> str:
    """Payload for the (unverified) ``rate_req`` DataTransfer. Mirrors the
    accepted qrcode_req dialect (snake_case + connector_id/evse_id) with the
    DefaultPrice price fields; the conf's ``result`` tells us whether a given
    firmware actually takes it (0 = accepted, -1 = refused)."""
    return json.dumps(
        {
            "connector_id": evse.ocpp_evse_id or 1,
            "evse_id": evse.evse_id or "",
            "flat_fee": float(tariff.price_session or 0.0),
            "hour_price": float(tariff.price_minute or 0.0) * 60,
            "kwh_price": float(tariff.price_kwh or 0.0),
            "unit": f"{(tariff.currency or '').upper()}/kWh",
        },
        separators=(",", ":"),
    )


async def push_rates(ocpp, db, evse) -> None:
    """Sync the connector tariff to an RCD charger. Called after the standing
    QR push on boot and after a catalog sync; both callers treat this as
    best-effort. DefaultPrice is station-global: on a dual-gun unit whose
    connectors carry different tariffs the last-synced EVSE wins the screen
    (rate_req stays per-connector, hence still sent per EVSE)."""
    try:
        if not is_rcd_evse(evse):
            return
        tariff = _connector_tariff(evse)
        if tariff is None:
            debug(" [rcd] no tariff wired to %s; rate push skipped", evse.evse_id)
            return

        _, protocol = ocpp._station_vendor(db, evse.station_id, evse.tenant_id)
        if (protocol or "").strip().lower().startswith("ocpp1.6"):
            value = build_default_price_value(tariff)
            response = ocpp.send_citrineos_message(
                station_id=evse.station_id,
                tenant_id=evse.tenant_id,
                url_path="configuration/changeConfiguration",
                json_payload={"key": "DefaultPrice", "value": value},
            )
            info(
                " [rcd] DefaultPrice pushed to %s (%s): %s",
                evse.station_id,
                evse.evse_id,
                getattr(response, "text", None),
            )

        response = ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/dataTransfer",
            json_payload={
                "vendorId": RCD_VENDOR_ID,
                "messageId": RATE_MESSAGE_ID,
                "data": build_rate_req_data(evse, tariff),
            },
        )
        info(
            " [rcd] rate_req sent to %s (%s): %s -- charger conf (incl. the"
            " result code for the unverified schema) is in the citrine log",
            evse.station_id,
            evse.evse_id,
            getattr(response, "text", None),
        )
    except Exception as e:
        warning(" [rcd] rate push failed for %s: %r", evse.evse_id, e.__str__())


def _zone_offset_string(tz_name: str) -> "str | None":
    """Current UTC offset of an IANA zone in the firmware's exact
    ``UTC%c%d:%d`` format -- sign, UNPADDED hours, colon, 2-digit minutes
    (UTC-7:00, UTC+5:30). None for an unknown zone. Computed at call time,
    so a boot after a DST flip pushes the new offset."""
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
    except Exception:
        return None
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds()) // 60
    sign = "+" if total_minutes >= 0 else "-"
    return f"UTC{sign}{abs(total_minutes) // 60}:{abs(total_minutes) % 60:02d}"


async def push_zone_offset(ocpp, db, evse) -> None:
    """Sync the display timezone to an RCD charger (its wall clock renders
    UTC + this offset; the UTC clock itself rides Boot/Heartbeat currentTime).
    Live-verified on fw V72.78: {"zoneOffset":"UTC-7:00"} -> result 0. The
    firmware refuses the change mid-session ("should not update zone offset
    where charging station!"), so this rides the boot flow while idle.
    Station-global -- call once per station, not per EVSE. Best-effort."""
    try:
        if not is_rcd_evse(evse):
            return
        tz_name = (Config.CHARGER_DISPLAY_TIMEZONE or "").strip()
        if not tz_name:
            return
        zone = _zone_offset_string(tz_name)
        if zone is None:
            warning(" [rcd] unknown CHARGER_DISPLAY_TIMEZONE %r", tz_name)
            return
        response = ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/dataTransfer",
            json_payload={
                "vendorId": RCD_VENDOR_ID,
                "messageId": ZONE_OFFSET_MESSAGE_ID,
                "data": json.dumps({"zoneOffset": zone}, separators=(",", ":")),
            },
        )
        info(
            " [rcd] zone offset %s pushed to %s: %s",
            zone,
            evse.station_id,
            getattr(response, "text", None),
        )
    except Exception as e:
        warning(
            " [rcd] zone offset push failed for %s: %r",
            evse.station_id,
            e.__str__(),
        )


# ---------------------------------------------------------------------------
# CP -> CS: realtime_status / bill
# ---------------------------------------------------------------------------


def _parse_data(raw):
    """DataTransfer ``data`` is specified as a JSON string but arrives as a
    dict from some stacks; accept both, never raise."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _field(data: dict, *names, cast=None):
    """First present field among camelCase/snake_case spellings, optionally
    cast; None when absent or uncastable."""
    for name in names:
        if name in data and data[name] is not None:
            if cast is None:
                return data[name]
            try:
                return cast(data[name])
            except (TypeError, ValueError):
                return None
    return None


async def handle_data_transfer(ocpp, payload: dict, station_id: str) -> None:
    """Charger-initiated DataTransfer from the AMQP stream. Non-rcd vendors
    and unknown operations are ignored (the core owns the OCPP reply; we only
    consume a copy)."""
    try:
        if (payload.get("vendorId") or "").strip().lower() != RCD_VENDOR_ID:
            return
        message_id = (payload.get("messageId") or "").strip().lower()
        data = _parse_data(payload.get("data"))
        if message_id.startswith("realtime_status"):
            await _handle_realtime_status(ocpp, data, station_id)
        elif message_id.startswith("bill"):
            _handle_bill(ocpp, data, station_id)
        else:
            debug(
                " [rcd] unhandled DataTransfer %r from %s", message_id, station_id
            )
    except Exception as e:
        # A vendor-payload surprise must never bounce the consumer loop.
        warning(
            " [rcd] DataTransfer handling failed for %s: %r",
            station_id,
            e.__str__(),
        )


def _tx_serial(data: dict):
    """The transaction serial in a realtime/bill payload -- RCD's per-station
    1.6 transactionId. 0 / missing = no transaction (idle telemetry)."""
    serial = _field(data, "serial", "transactionId", "transaction_id")
    if serial is None or str(serial) == "0":
        return None
    return str(serial)


async def _handle_realtime_status(ocpp, data: dict, station_id: str) -> None:
    serial = _tx_serial(data)
    info(
        " [rcd] realtime_status from %s: serial=%s energy=%s amount=%s soc=%s"
        " charge_time=%s",
        station_id,
        serial,
        _field(data, "electricEnergy", "electric_energy"),
        _field(data, "amount"),
        _field(data, "soc", "stopSoc", "stop_soc"),
        _field(data, "chargeTime", "charge_time"),
    )
    if serial is None:
        return

    soc = _field(data, "soc", "stopSoc", "stop_soc", cast=float)
    if soc is None:
        return

    # Only SoC rides the session pipeline (see module docstring on why the
    # energy figure must not touch the register-delta accumulator).
    synthetic = TransactionEventRequest(
        eventType=TransactionEventEnumType.Updated,
        # The firmware's timestamp strings aren't reliably ISO; the sample
        # time is not billing-relevant here (SoC only), so stamp receipt time.
        timestamp=datetime.now(timezone.utc),
        triggerReason=TriggerReasonEnumType.MeterValuePeriodic,
        transactionInfo=TransactionType(transactionId=serial),
        meterValue=[
            MeterValueType(
                sampledValue=[
                    SampledValueType(value=soc, measurand=MeasurandEnumType.SoC)
                ]
            )
        ],
    )
    await ocpp.process_transaction_updated(
        transaction_event=synthetic, station_id=station_id
    )


def _handle_bill(ocpp, data: dict, station_id: str) -> None:
    """End-of-session billing record: log it whole, and reconcile the
    charger's session totals against our checkout. Read-only on purpose --
    billing stays driven by our own meter pipeline; the bill is the vendor's
    second opinion."""
    serial = _tx_serial(data)
    info(" [rcd] bill from %s: %s", station_id, json.dumps(data, default=str))
    if serial is None:
        return

    db = next(get_db())
    probe = TransactionEventRequest(
        eventType=TransactionEventEnumType.Ended,
        timestamp=datetime.now(timezone.utc),
        triggerReason=TriggerReasonEnumType.EVDeparted,
        transactionInfo=TransactionType(transactionId=serial),
    )
    checkout = ocpp.find_checkout_for_event(db, probe, station_id=station_id)
    if checkout is None:
        info(
            " [rcd] bill serial %s on %s matches no checkout (free-vend or"
            " RFID session?)",
            serial,
            station_id,
        )
        return

    charger_kwh = _field(data, "electricEnergy", "electric_energy", cast=float)
    our_kwh = checkout.transaction_kwh
    if charger_kwh is not None and our_kwh is not None:
        drift = abs(charger_kwh - our_kwh)
        if drift > max(BILL_ENERGY_TOLERANCE_KWH, 0.02 * charger_kwh):
            warning(
                " [rcd] bill/meter energy drift on %s (checkout %s): charger"
                " says %.3f kWh, meter pipeline says %.3f kWh",
                station_id,
                checkout.id,
                charger_kwh,
                our_kwh,
            )
    debug(
        " [rcd] bill reconciled for checkout %s: charger amount=%s energy=%s",
        checkout.id,
        _field(data, "amount"),
        charger_kwh,
    )
