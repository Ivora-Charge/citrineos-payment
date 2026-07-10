"""Fleet background tasks (multi-tenant rollout Phase 7).

Two periodic loops, both defensive (an iteration failure is logged and the
loop keeps running):

Reaper -- settle stuck sessions before the Stripe hold expires.
    A charger that dies (or never reconnects) mid-session leaves a checkout
    with a hold but no TransactionEvent(Ended) to trigger capture. Manual
    card-not-present holds expire after ~7 days, after which the money is
    simply lost. Any un-captured checkout whose session started more than
    REAPER_STALE_HOURS ago is settled from its last-known state: the end time
    is taken from the core Transaction row's updatedAt (the last packet the
    CSMS saw -- never now(), which would inflate time-based cost), and the
    capture goes through the normal pipeline, which is idempotent
    (captured_at + PaymentIntent status guards).

Offline/fault alerting -- tell support before the customer calls.
    Scans the shared CitrineOS tables for stations that are offline or
    Faulted and POSTs a Slack-compatible {"text": ...} payload to
    ALERT_WEBHOOK_URL. Alerts are edge-triggered per station (re-armed when
    the station recovers), with in-memory state -- a service restart may
    re-send one round of alerts, which is acceptable.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from logging import error, info, warning

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Checkout, get_db


async def reaper_loop(ocpp_integration) -> None:
    if not Config.REAPER_ENABLED:
        info(" [reaper] disabled (REAPER_ENABLED=false)")
        return
    interval = max(Config.REAPER_INTERVAL_MINUTES, 1) * 60
    info(
        f" [reaper] settling checkouts stuck > {Config.REAPER_STALE_HOURS}h, "
        f"every {Config.REAPER_INTERVAL_MINUTES}m"
    )
    while True:
        try:
            await _reap_once(ocpp_integration)
        except Exception as e:  # noqa: BLE001 -- keep the loop alive
            error(f" [reaper] iteration failed: {e}")
        try:
            _close_hung_transactions()
        except Exception as e:  # noqa: BLE001
            error(f" [janitor] iteration failed: {e}")
        await asyncio.sleep(interval)


async def _reap_once(ocpp_integration) -> None:
    # Close the session before sleeping: a session held across the sleep sits
    # "idle in transaction" and blocks any DDL (and everything queued behind
    # it) for the whole interval.
    db: Session = next(get_db())
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=Config.REAPER_STALE_HOURS)
        stuck = (
            db.query(Checkout)
            .filter(
                Checkout.payment_intent_id.isnot(None),
                Checkout.captured_at.is_(None),
                Checkout.transaction_end_time.is_(None),
                Checkout.transaction_start_time.isnot(None),
                Checkout.transaction_start_time < cutoff,
            )
            .all()
        )
        for checkout in stuck:
            # End basis: the last packet the CSMS recorded for this session
            # (core Transactions.updatedAt), never the current time.
            row = db.execute(
                text(
                    'SELECT "updatedAt" FROM "Transactions" '
                    'WHERE "remoteStartId" = :cid '
                    'OR "transactionId" = :tid '
                    'ORDER BY "updatedAt" DESC LIMIT 1'
                ),
                {
                    "cid": checkout.id,
                    "tid": checkout.remote_request_transaction_id or "",
                },
            ).first()
            end_time = row[0] if row and row[0] else checkout.transaction_start_time
            warning(
                f" [reaper] settling stuck checkout {checkout.id} "
                f"(started {checkout.transaction_start_time}, last seen {end_time})"
            )
            checkout.transaction_end_time = end_time
            db.add(checkout)
            db.commit()
            await ocpp_integration.capture_payment_transaction(checkout_id=checkout.id)
    finally:
        db.close()


def _close_hung_transactions() -> None:
    """Flip isActive off on core Transactions whose station has been offline
    longer than JANITOR_OFFLINE_HOURS.

    A charger that dies (or is swapped out) mid-session never sends the
    closing StopTransaction / TransactionEvent(Ended), and CitrineOS waits
    forever -- the operator UI shows the session "Active" indefinitely (two
    June sims still showed active sessions during the 2026-07-08 trial). The
    money side is unaffected: the reaper settles the attached checkout from
    its last-seen packet independently, and Transactions.updatedAt is left
    untouched so that end-basis stays honest. A late StopTransaction from a
    reconnecting charger still bills normally (checkout matching is by
    transaction id, not by isActive)."""
    if Config.JANITOR_OFFLINE_HOURS <= 0:
        return
    db: Session = next(get_db())
    try:
        closed = db.execute(
            text(
                'UPDATE "Transactions" t SET "isActive" = false '
                'FROM "ChargingStations" cs '
                'WHERE t."stationId" = cs.id AND t."isActive" IS TRUE '
                'AND cs."isOnline" = false '
                "AND cs.\"updatedAt\" < NOW() - (:hrs || ' hours')::interval "
                'RETURNING t."transactionId", cs."ocppConnectionName"'
            ),
            {"hrs": Config.JANITOR_OFFLINE_HOURS},
        ).fetchall()
        # Orphans: stationId NULL (station deleted, or the row was created in
        # the boot/registration race and never attached). No station will ever
        # close these -- 33 of them sat "Active" from a 2026-07-06 load test.
        orphaned = db.execute(
            text(
                'UPDATE "Transactions" SET "isActive" = false '
                'WHERE "isActive" IS TRUE AND "stationId" IS NULL '
                "AND \"updatedAt\" < NOW() - (:hrs || ' hours')::interval "
                'RETURNING "transactionId"'
            ),
            {"hrs": Config.JANITOR_OFFLINE_HOURS},
        ).fetchall()
        db.commit()
        for tx_id, station in closed:
            warning(
                f" [janitor] closed hung transaction {tx_id} on {station} "
                f"(station offline > {Config.JANITOR_OFFLINE_HOURS}h)"
            )
        if orphaned:
            warning(
                f" [janitor] closed {len(orphaned)} orphaned transaction(s) "
                f"with no station (deleted or never attached)"
            )
    finally:
        db.close()


# station name -> problem string currently alerted on (edge-triggered)
_alerted: dict[str, str] = {}


async def alert_loop() -> None:
    if not Config.ALERT_WEBHOOK_URL:
        info(" [alerts] disabled (ALERT_WEBHOOK_URL not set)")
        return
    interval = max(Config.ALERT_INTERVAL_MINUTES, 1) * 60
    info(
        f" [alerts] watching for offline > {Config.ALERT_OFFLINE_MINUTES}m / "
        f"Faulted stations, every {Config.ALERT_INTERVAL_MINUTES}m"
    )
    while True:
        try:
            _alert_once()
        except Exception as e:  # noqa: BLE001
            error(f" [alerts] iteration failed: {e}")
        await asyncio.sleep(interval)


def _alert_once() -> None:
    db: Session = next(get_db())
    try:
        _scan_and_alert(db)
    finally:
        db.close()


def _scan_and_alert(db: Session) -> None:
    problems: dict[str, str] = {}

    offline = db.execute(
        text(
            'SELECT cs."ocppConnectionName", t.name FROM "ChargingStations" cs '
            'LEFT JOIN "Tenants" t ON t.id = cs."tenantId" '
            'WHERE cs."isOnline" = false '
            "AND cs.\"updatedAt\" < NOW() - (:mins || ' minutes')::interval"
        ),
        {"mins": Config.ALERT_OFFLINE_MINUTES},
    ).fetchall()
    for name, tenant in offline:
        problems[name] = f"offline > {Config.ALERT_OFFLINE_MINUTES}m (tenant: {tenant or '—'})"

    faulted = db.execute(
        text(
            'SELECT DISTINCT sn."ocppConnectionName" FROM "LatestStatusNotifications" lsn '
            'JOIN "StatusNotifications" sn ON sn.id = lsn."statusNotificationId" '
            "WHERE sn.\"connectorStatus\" = 'Faulted'"
        )
    ).fetchall()
    for (name,) in faulted:
        problems[name] = (problems.get(name, "") + " Faulted").strip()

    # fire on new/changed problems, re-arm on recovery
    for name in list(_alerted):
        if name not in problems:
            _send(f"✅ Charger {name} recovered ({_alerted[name]})")
            del _alerted[name]
    for name, problem in problems.items():
        if _alerted.get(name) != problem:
            _send(f"🔴 Charger {name}: {problem}")
            _alerted[name] = problem


def _send(message: str) -> None:
    try:
        requests.post(Config.ALERT_WEBHOOK_URL, json={"text": message}, timeout=10)
        info(f" [alerts] sent: {message}")
    except Exception as e:  # noqa: BLE001
        error(f" [alerts] webhook failed: {e}")
