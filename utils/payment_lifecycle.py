"""Durable cancellation and Stripe reconciliation shared by API and workers."""

from datetime import datetime, timedelta, timezone

import stripe
from sqlalchemy import text

from config import Config
from db.init_db import Checkout, Connector, Evse, Location, Operator
from utils import live
from utils.utils import stripe_account_kwargs


def utc(value):
    return (
        value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    )


def inactivity_deadline(checkout):
    basis = checkout.last_activity_at or checkout.authorized_at
    return (
        utc(basis) + timedelta(seconds=Config.PAYMENT_INACTIVITY_SECONDS)
        if basis
        else None
    )


def checkout_evse(db, checkout):
    return (
        db.query(Evse)
        .join(Connector, Connector.evse_id == Evse.id)
        .filter(Connector.id == checkout.connector_id)
        .first()
    )


def checkout_account(db, checkout):
    if checkout.stripe_account_id:
        return checkout.stripe_account_id
    operator = (
        db.query(Operator)
        .join(Location, Location.operator_id == Operator.id)
        .join(Evse, Evse.location_id == Location.id)
        .join(Connector, Connector.evse_id == Evse.id)
        .filter(Connector.id == checkout.connector_id)
        .first()
    )
    if operator is None:
        raise ValueError(f"Cannot resolve Stripe account for checkout {checkout.id}")
    checkout.stripe_account_id = operator.stripe_account_id
    return operator.stripe_account_id


def locked_checkout(db, checkout_id):
    return (
        db.query(Checkout)
        .filter(Checkout.id == checkout_id)
        .populate_existing()
        .with_for_update()
        .one()
    )


def reconcile_core_transaction(db, checkout):
    """Recover a start/end lost by the payment consumer before releasing money."""
    if db.get_bind().dialect.name != "postgresql":
        return
    evse = checkout_evse(db, checkout)
    if evse is None:
        return
    row = db.execute(
        text(
            'SELECT t."transactionId", t."startTime", t."endTime", t."totalKwh", '
            't."updatedAt", t."meterStart" FROM "Transactions" t '
            'JOIN "ChargingStations" s ON s.id=t."stationId" '
            'LEFT JOIN "Authorizations" a ON a.id=t."authorizationId" '
            'WHERE s."ocppConnectionName"=:station AND s."tenantId"=:tenant '
            'AND (t."remoteStartId"=:cid OR a."idToken"=:token '
            'OR t."transactionId"=:txid) ORDER BY t."createdAt" DESC LIMIT 1'
        ),
        {
            "station": evse.station_id,
            "tenant": int(evse.tenant_id),
            "cid": checkout.id,
            "token": f"{Config.OCPP_REMOTESTART_IDTAG_PREFIX}{checkout.id}",
            "txid": checkout.remote_request_transaction_id,
        },
    ).first()
    if not row:
        return
    checkout.remote_request_transaction_id = row[0]
    checkout.transaction_start_time = checkout.transaction_start_time or row[1]
    checkout.transaction_end_time = checkout.transaction_end_time or row[2]
    if row[3] is not None and float(row[3]) > (checkout.transaction_kwh or 0):
        checkout.transaction_kwh = float(row[3])
        checkout.last_activity_at = row[4]
        if row[5] is not None:
            # Core Transactions.meterStart is already kWh (unlike the raw
            # OCPP 1.6 StartTransaction reading, which is Wh). Dividing again
            # makes the next meter packet bill the charger's lifetime energy.
            checkout.transaction_last_meter_reading = float(row[5]) + float(row[3])


def revoke_authorization(db, checkout):
    # Tests use SQLite without the core tables; production and development
    # share the Postgres Authorizations table with CitrineOS.
    if db.get_bind().dialect.name != "postgresql":
        return
    evse = checkout_evse(db, checkout)
    if evse is not None:
        db.execute(
            text(
                'UPDATE "Authorizations" SET status=\'Blocked\', "updatedAt"=now() '
                'WHERE "tenantId"=:tenant AND "idToken"=:token'
            ),
            {
                "tenant": int(evse.tenant_id),
                "token": f"{Config.OCPP_REMOTESTART_IDTAG_PREFIX}{checkout.id}",
            },
        )


def record_stripe_terminal(checkout, intent):
    """Return false for a transient state: it must remain eligible for retries."""
    if intent.status not in ("canceled", "succeeded"):
        return False
    checkout.captured_at = datetime.now(timezone.utc)
    checkout.captured_amount = int(getattr(intent, "amount_received", 0) or 0)
    if intent.status == "canceled":
        checkout.canceled_at = checkout.captured_at
    return True


def release_checkout(db, checkout_id):
    """Release a previously requested cancellation. Errors leave it retryable.

    The row lock serializes this with capture and late Started events. Stripe
    idempotency plus retrieval cover a crash between the API call and commit.
    """
    checkout = locked_checkout(db, checkout_id)
    if checkout.captured_at is not None:
        return checkout
    if not checkout.cancellation_requested_at:
        raise ValueError("Cancellation must be persisted before releasing a hold")
    account = checkout_account(db, checkout)
    kwargs = stripe_account_kwargs(account)
    if not checkout.payment_intent_id and checkout.stripe_checkout_session_id:
        session = stripe.checkout.Session.retrieve(
            checkout.stripe_checkout_session_id, **kwargs
        )
        if session.status == "open":
            try:
                session = stripe.checkout.Session.expire(session.id, **kwargs)
            except stripe.error.InvalidRequestError:
                # Payment may have completed between retrieve and expire.
                session = stripe.checkout.Session.retrieve(session.id, **kwargs)
                if session.status == "open":
                    raise
        checkout.payment_intent_id = session.payment_intent
    if checkout.payment_intent_id:
        intent = stripe.PaymentIntent.retrieve(checkout.payment_intent_id, **kwargs)
        if intent.status not in ("canceled", "succeeded"):
            try:
                intent = stripe.PaymentIntent.cancel(
                    checkout.payment_intent_id,
                    idempotency_key=f"checkout-{checkout.id}-cancel-{checkout.payment_intent_id}",
                    **kwargs,
                )
            except stripe.error.InvalidRequestError:
                intent = stripe.PaymentIntent.retrieve(
                    checkout.payment_intent_id, **kwargs
                )
                if intent.status not in ("canceled", "succeeded"):
                    raise
        if not record_stripe_terminal(checkout, intent):
            raise RuntimeError("Stripe has not completed cancellation")
    else:
        checkout.canceled_at = datetime.now(timezone.utc)
        checkout.captured_at = checkout.canceled_at
        checkout.captured_amount = 0
    revoke_authorization(db, checkout)
    db.commit()
    live.notify(checkout.id)
    return checkout


def cancel_checkout(db, checkout_id, reason, *, prestart_only=False):
    checkout = locked_checkout(db, checkout_id)
    if checkout.captured_at is not None:
        return checkout
    if prestart_only:
        reconcile_core_transaction(db, checkout)
        if checkout.transaction_start_time is not None:
            db.commit()
            return checkout
    checkout.cancellation_requested_at = (
        checkout.cancellation_requested_at or datetime.now(timezone.utc)
    )
    checkout.cancellation_reason = checkout.cancellation_reason or reason
    revoke_authorization(db, checkout)
    db.commit()  # Persist intent even when Stripe is unavailable.
    live.notify(checkout.id)
    return release_checkout(db, checkout.id)
