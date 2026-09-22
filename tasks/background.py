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
from contextlib import closing
from datetime import datetime, timedelta, timezone
from logging import error, info, warning

import requests
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Checkout, get_db
from utils.payment_lifecycle import (
    cancel_checkout,
    checkout_account,
    checkout_evse,
    inactivity_deadline,
    locked_checkout,
    release_checkout,
    record_stripe_terminal,
    utc,
    reconcile_core_transaction,
)
from utils.utils import stripe_account_kwargs
from utils import live
import stripe


async def payment_recovery_loop(ocpp_integration):
    """Independent of the 48-hour legacy reaper; runs without a browser open."""
    while True:
        try:
            await recover_payments_once(ocpp_integration)
        except Exception:
            error(" [payments] recovery iteration failed", exc_info=True)
        await asyncio.sleep(max(1, Config.PAYMENT_RECOVERY_INTERVAL_SECONDS))


async def recover_payments_once(ocpp_integration, now=None):
    now = now or datetime.now(timezone.utc)
    with closing(next(get_db())) as db:
        ids = [
            row[0]
            for row in db.query(Checkout.id)
            .filter(
                or_(
                    Checkout.stop_requested_at.isnot(None),
                    Checkout.captured_at.is_(None)
                    & or_(
                        Checkout.payment_intent_id.isnot(None),
                        Checkout.source == "free",
                        Checkout.cancellation_requested_at.isnot(None),
                    ),
                ),
            )
            .all()
        ]
    # A failure for one hold must not prevent other drivers' releases.
    for checkout_id in ids:
        try:
            await _recover_checkout(ocpp_integration, checkout_id, now)
        except Exception:
            error(
                " [payments] recovery failed for checkout %s; will retry",
                checkout_id,
                exc_info=True,
            )


async def _recover_checkout(ocpp, checkout_id, now):
    settle = False
    with closing(next(get_db())) as db:
        checkout = locked_checkout(db, checkout_id)
        if checkout.captured_at is not None:
            # Releasing money must not abandon a stop when the charger was
            # offline. Keep retrying until its real transaction has ended.
            if checkout.stop_requested_at:
                _retry_settled_stop(db, checkout, ocpp)
            return
        if checkout.cancellation_requested_at:
            release_checkout(db, checkout_id)
            return
        reconcile_core_transaction(db, checkout)
        db.commit()
        checkout = locked_checkout(db, checkout_id)
        # Backfill old authorized holds from Stripe's original timestamp, not
        # deployment time. This also repairs canceled/expired local markers.
        if checkout.authorized_at is None:
            if checkout.payment_intent_id:
                intent = stripe.PaymentIntent.retrieve(
                    checkout.payment_intent_id,
                    **stripe_account_kwargs(checkout_account(db, checkout)),
                )
                if record_stripe_terminal(checkout, intent):
                    db.commit()
                    live.notify(checkout_id)
                    return
                checkout.authorized_at = datetime.fromtimestamp(
                    intent.created, timezone.utc
                )
            else:
                checkout.authorized_at = (
                    checkout.transaction_start_time or checkout.created_at
                )
            db.commit()
            checkout = locked_checkout(db, checkout_id)
        if checkout.transaction_end_time is not None:
            # Unlike the old reaper, retry a completed session after a failed
            # capture. Its real end timestamp and energy remain unchanged.
            settle = True
        elif checkout.remote_request_status == "Rejected":
            cancel_checkout(db, checkout_id, "start_failed")
            return
        else:
            deadline = inactivity_deadline(checkout)
            idle = deadline is not None and now >= deadline
            if not idle and not checkout.stop_requested_at:
                return
            if checkout.transaction_start_time is None:
                cancel_checkout(db, checkout_id, "inactivity", prestart_only=True)
                return
            evse = checkout_evse(db, checkout)
            # Retain the stop request even when the charger is offline.
            checkout.stop_requested_at = checkout.stop_requested_at or now
            if idle and not checkout.cancellation_reason:
                checkout.cancellation_reason = "inactivity"
            txid = checkout.remote_request_transaction_id
            # Release this lock before the message sender does other DB reads.
            db.commit()
            if txid and evse:
                ocpp.send_citrineos_message(
                    station_id=evse.station_id,
                    tenant_id=evse.tenant_id,
                    url_path="evdriver/requestStopTransaction",
                    json_payload={"transactionId": txid},
                )
            checkout = locked_checkout(db, checkout_id)
            if checkout.captured_at is not None:
                return
            if not idle:
                return  # Ordinary manual stop: await the final meter reading.
            if not checkout.transaction_kwh or checkout.transaction_kwh <= 0:
                cancel_checkout(db, checkout_id, "inactivity")
                return
            # Five minutes without positive meter activity: bill only the last
            # recorded usage and release the unused authorization now, even if
            # the charger cannot deliver an Ended event. Never bill idle time.
            checkout.transaction_end_time = utc(
                checkout.last_activity_at or checkout.transaction_start_time
            )
            db.commit()
            settle = True
    if settle:
        await ocpp.capture_payment_transaction(checkout_id=checkout_id)


def _retry_settled_stop(db, checkout, ocpp):
    evse = checkout_evse(db, checkout)
    if not evse:
        return
    if db.get_bind().dialect.name == "postgresql":
        row = db.execute(
            text(
                'SELECT t."transactionId", t."isActive", t."endTime" '
                'FROM "Transactions" t JOIN "ChargingStations" s ON s.id=t."stationId" '
                'LEFT JOIN "Authorizations" a ON a.id=t."authorizationId" '
                'WHERE s."ocppConnectionName"=:station AND s."tenantId"=:tenant '
                'AND (a."idToken"=:token OR t."transactionId"=:txid) '
                'ORDER BY t."createdAt" DESC LIMIT 1'
            ),
            {
                "station": evse.station_id,
                "tenant": int(evse.tenant_id),
                "token": f"{Config.OCPP_REMOTESTART_IDTAG_PREFIX}{checkout.id}",
                "txid": checkout.remote_request_transaction_id,
            },
        ).first()
        if row:
            if not row[1] or row[2]:
                checkout.stop_requested_at = None
                db.commit()
                return
            checkout.remote_request_transaction_id = row[0]
    txid = checkout.remote_request_transaction_id
    db.commit()
    if txid:
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="evdriver/requestStopTransaction",
            json_payload={"transactionId": txid},
        )


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
                # Paid sessions (a hold to rescue) and admin free sessions
                # (no hold, but the page would show "charging" forever).
                or_(Checkout.payment_intent_id.isnot(None), Checkout.source == "free"),
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
        problems[name] = (
            f"offline > {Config.ALERT_OFFLINE_MINUTES}m (tenant: {tenant or '—'})"
        )

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
