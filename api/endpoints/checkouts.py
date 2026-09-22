import asyncio
from datetime import datetime, timezone
from logging import error, info

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from utils.platform_fees import checkout_rate
from config import Config
from db.init_db import (
    get_db,
    SessionLocal,
    Connector as ConnectorModel,
    Evse as EvseModel,
    Tariff as TariffModel,
    Location as LocationModel,
    Checkout as CheckoutModel,
)

from schemas.checkouts import (
    Checkout,
    CheckoutCreate,
    CheckoutCreateResponse,
    FreeCheckoutCreate,
    FreeCheckoutResponse,
    RequestStartStopStatusEnumType,
)
from utils import free_charge, live
from utils.utils import generate_pricing, stripe_account_kwargs
from utils.payment_lifecycle import (
    cancel_checkout,
    locked_checkout,
    inactivity_deadline,
    reconcile_core_transaction,
)

router = APIRouter()

# SSE refresh cadence between charger events: keeps the charging clock and
# time-based costs ticking smoothly on the page (charger data itself only
# changes per MeterValue), and keeps intermediaries (nginx's default 60s
# proxy_read_timeout) from cutting an idle stream. Each tick is one snapshot
# (a short DB session + pricing calc) per connected client.
EVENTS_SNAPSHOT_SECONDS = 2


@router.post("/", response_model=CheckoutCreateResponse)
def create_checkout(request_body: CheckoutCreate, db: Session = Depends(get_db)):
    evse = db.query(EvseModel).filter(EvseModel.evse_id == request_body.evse_id).first()
    if evse is None:
        raise HTTPException(status_code=404, detail="EVSE not found")
    if getattr(evse, "retired_at", None) is not None:
        raise HTTPException(
            status_code=410, detail="This charger is no longer in service"
        )

    tariff = (
        db.query(TariffModel)
        .filter(TariffModel.id == evse.connectors[0].tariff_id)
        .first()
    )
    if tariff is None:
        raise HTTPException(status_code=404, detail="No Tariff for EVSE found")

    location = (
        db.query(LocationModel).filter(LocationModel.id == evse.location_id).first()
    )
    if location is None:
        raise HTTPException(status_code=404, detail="No Location for EVSE found")

    db_checkout = CheckoutModel(
        connector_id=evse.connectors[0].id,
        tariff_id=tariff.id,
        stripe_account_id=location.operator.stripe_account_id,
        platform_fee_bps=checkout_rate(db, evse, location.operator.stripe_account_id),
    )
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)

    checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": tariff.currency.lower(),
                    "product_data": {"name": "Charging Session Authorization Amount"},
                    "unit_amount": int(tariff.authorization_amount * 100),
                    "tax_behavior": "inclusive",
                },
                "quantity": 1,
            },
        ],
        metadata={"checkoutId": db_checkout.id},
        # Save the card (Stripe vaults it under a Customer; we keep only the
        # tokens on the PaymentIntent) so settlement can bill any cost above the
        # hold as a second off-session "overage" charge. Stripe Checkout shows the
        # off-session mandate consent automatically.
        customer_creation="always",
        payment_intent_data={
            "capture_method": "manual",
            "setup_future_usage": "off_session",
        },
        **stripe_account_kwargs(location.operator.stripe_account_id),
        mode="payment",
        success_url=f"{request_body.success_url}/{db_checkout.id}",
        cancel_url=request_body.cancel_url,
    )
    db_checkout.payment_intent_id = checkout.payment_intent
    db_checkout.stripe_checkout_session_id = getattr(checkout, "id", None)
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)

    return CheckoutCreateResponse(
        id=db_checkout.id,
        url=checkout.url,
    )


def _free_tariff(db: Session, currency: str) -> TariffModel:
    """The all-zero tariff free sessions are priced with, one per currency.
    Pricing, the charging page, the receipt and the revenue stats all read
    the checkout's tariff, so a zero tariff keeps every one of them honest
    without special cases."""
    values = {
        "currency": currency,
        "tax_rate": 0.0,
        "authorization_amount": 0.0,
        "price_kwh": 0.0,
        "price_minute": 0.0,
        "price_session": 0.0,
        "payment_fee": 0.0,
    }
    tariff = db.query(TariffModel).filter_by(**values).first()
    if tariff is None:
        tariff = TariffModel(**values)
        db.add(tariff)
        db.flush()
    return tariff


@router.post("/free", response_model=FreeCheckoutResponse)
async def start_free_checkout(
    request_body: FreeCheckoutCreate, request: Request, db: Session = Depends(get_db)
):
    """Admin free charging: start a session on the EVSE with the charger's
    free-charge password instead of a card.

    The host enables this per charger in Charger Management (platform-api
    pushes the flag and password hash with the catalog sync). The session is
    a normal remote start tied to a checkout with no PaymentIntent and a
    zero tariff, so the charging page, settlement (which skips Stripe when
    there is no hold) and reporting all work unchanged; it shows up as a
    session with zero revenue."""
    evse = db.query(EvseModel).filter(EvseModel.evse_id == request_body.evse_id).first()
    if evse is None:
        raise HTTPException(status_code=404, detail="EVSE not found")
    if getattr(evse, "retired_at", None) is not None:
        raise HTTPException(
            status_code=410, detail="This charger is no longer in service"
        )
    if not evse.free_charge_enabled or not evse.free_charge_password_hash:
        raise HTTPException(status_code=404, detail="checkout.free.error.unavailable")
    if not evse.connectors:
        raise HTTPException(status_code=404, detail="No connector for EVSE found")

    if free_charge.is_locked(evse.evse_id):
        raise HTTPException(status_code=429, detail="checkout.free.error.locked")
    if not free_charge.verify_password(
        request_body.password, evse.free_charge_password_hash
    ):
        free_charge.record_failure(evse.evse_id)
        raise HTTPException(status_code=401, detail="checkout.free.error.password")
    free_charge.clear_failures(evse.evse_id)

    paid_tariff = (
        db.query(TariffModel)
        .filter(TariffModel.id == evse.connectors[0].tariff_id)
        .first()
    )
    currency = (paid_tariff.currency if paid_tariff is not None else "usd").lower()
    tariff = _free_tariff(db, currency)
    db_checkout = CheckoutModel(
        connector_id=evse.connectors[0].id,
        tariff_id=tariff.id,
        source="free",
        authorization_amount=0,
        authorized_at=datetime.now(timezone.utc),
    )
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)

    # Same remote-start shape as the paid web flow (webhooks.handle_web_portal):
    # a PAY_<checkout> idTag in the station's tenant, so 1.6 chargers link
    # the session back to this checkout through the idTag prefix too.
    ocpp_integration = request.app.ocpp_integration
    authorization = await ocpp_integration.create_authorization(
        f"{Config.OCPP_REMOTESTART_IDTAG_PREFIX}{db_checkout.id}",
        "Central",
        [(str(db_checkout.id), "FreeChargeCheckoutId")],
        tenant_id=evse.tenant_id,
    )
    if authorization is None:
        error(" [free] unable to create authorization for checkout %s", db_checkout.id)
        raise HTTPException(status_code=502, detail="charging.error.generic")

    # Local import: webhooks.py is unrelated at import time but shares the app.
    from api.endpoints.webhooks import citrineos_call_succeeded

    response = ocpp_integration.send_citrineos_message(
        station_id=evse.station_id,
        tenant_id=evse.tenant_id,
        url_path="evdriver/requestStartTransaction",
        json_payload={
            "remoteStartId": db_checkout.id,
            "idToken": authorization["idToken"],
            "evseId": evse.ocpp_evse_id,
        },
    )
    db_checkout.remote_request_status = (
        RequestStartStopStatusEnumType.ACCEPTED
        if citrineos_call_succeeded(response)
        else RequestStartStopStatusEnumType.REJECTED
    )
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)
    live.notify(db_checkout.id)
    info(
        " [free] checkout %s on %s: remote start %s",
        db_checkout.id,
        evse.evse_id,
        db_checkout.remote_request_status,
    )
    return FreeCheckoutResponse(
        id=db_checkout.id, remote_request_status=db_checkout.remote_request_status
    )


def _checkout_snapshot(checkout_id: int) -> "Checkout | None":
    """The full checkout payload (pricing + live EVSE status) from a fresh,
    short-lived session. Used by the GET and by every SSE frame -- an event
    stream must never hold a pooled connection open for its lifetime."""
    db = SessionLocal()
    try:
        db_checkout = (
            db.query(CheckoutModel).filter(CheckoutModel.id == checkout_id).first()
        )
        if db_checkout is None:
            return None
        db_connector = (
            db.query(ConnectorModel)
            .filter(ConnectorModel.id == db_checkout.connector_id)
            .first()
        )
        db_evse = (
            db.query(EvseModel).filter(EvseModel.id == db_connector.evse_id).first()
            if db_connector
            else None
        )
        return Checkout(
            **{
                **db_checkout.__dict__,
                "pricing": generate_pricing(db_checkout.id, db=db),
                "evse_status": db_evse.status if db_evse is not None else None,
                "inactivity_deadline": inactivity_deadline(db_checkout),
            }
        )
    finally:
        db.close()


@router.get("/{id}", response_model=Checkout)
def get_checkout(id: int):
    output_checkout = _checkout_snapshot(id)
    if output_checkout is None:
        raise HTTPException(status_code=404, detail="charging.error.sessionnotfound")

    return output_checkout


@router.get("/{id}/events")
async def checkout_events(id: int):
    """Server-sent events stream of checkout snapshots for the charging page.

    Sends a snapshot immediately, then on every checkout change (meter values,
    session start/end, webhook status) via utils.live, plus a periodic refresh
    every EVENTS_SNAPSHOT_SECONDS. The stream ends once the transaction has an
    end time -- the client shows the final state and stops reconnecting."""
    if _checkout_snapshot(id) is None:
        raise HTTPException(status_code=404, detail="charging.error.sessionnotfound")

    async def stream():
        queue = live.subscribe(id)
        try:
            while True:
                snapshot = _checkout_snapshot(id)
                if snapshot is None:
                    break  # checkout vanished mid-stream; nothing left to say
                yield f"data: {snapshot.model_dump_json()}\n\n"
                if snapshot.captured_at is not None:
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=EVENTS_SNAPSHOT_SECONDS)
                except asyncio.TimeoutError:
                    pass  # heartbeat: fall through to a fresh snapshot
        finally:
            live.unsubscribe(id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tell nginx not to buffer this response -- avoids needing a
            # proxy_buffering directive in the (sudo-gated) site config.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{id}/stop")
def stop_checkout(id: int, request: Request, db: Session = Depends(get_db)):
    """Cancel and release a pending start, or stop an active charging session.

    Persist the request so recovery continues after a browser disconnect or
    temporary Stripe/charger failure. Active usage settles from the final meter.
    """
    db_checkout = db.query(CheckoutModel).filter(CheckoutModel.id == id).first()
    if db_checkout is None:
        raise HTTPException(status_code=404, detail="charging.error.sessionnotfound")
    db_checkout = locked_checkout(db, id)
    if db_checkout.captured_at is not None:
        return {"status": "Canceled" if db_checkout.canceled_at else "Closed"}
    reconcile_core_transaction(db, db_checkout)
    db.commit()
    db_checkout = locked_checkout(db, id)
    if db_checkout.transaction_start_time is None:
        try:
            checkout = cancel_checkout(db, id, "driver_canceled", prestart_only=True)
        except Exception:
            # The durable cancellation marker lets the recovery worker retry.
            db.rollback()
            error(" [payments] hold release pending for checkout %s", id, exc_info=True)
            return {"status": "Canceling"}
        if checkout.captured_at:
            return {"status": "Canceled" if checkout.canceled_at else "Closed"}
        db_checkout = checkout  # A start raced the cancellation: stop that session.
    if db_checkout.transaction_end_time is not None:
        return {"status": "Settling"}
    db_checkout.stop_requested_at = db_checkout.stop_requested_at or datetime.now(
        timezone.utc
    )
    db.commit()
    if db_checkout.remote_request_transaction_id is None:
        # A Started event can race the core transaction-ID insert. Keep the
        # request durable; the worker/late-bind handler will send the stop.
        return {"status": "Stopping"}

    db_connector = (
        db.query(ConnectorModel)
        .filter(ConnectorModel.id == db_checkout.connector_id)
        .first()
    )
    db_evse = (
        db.query(EvseModel).filter(EvseModel.id == db_connector.evse_id).first()
        if db_connector
        else None
    )
    if db_evse is None:
        raise HTTPException(status_code=404, detail="charging.error.sessionnotfound")

    # Local import: webhooks.py is unrelated at import time but shares the app.
    from api.endpoints.webhooks import citrineos_call_succeeded

    response = request.app.ocpp_integration.send_citrineos_message(
        station_id=db_evse.station_id,
        tenant_id=db_evse.tenant_id,
        url_path="evdriver/requestStopTransaction",
        json_payload={"transactionId": db_checkout.remote_request_transaction_id},
    )
    if not citrineos_call_succeeded(response):
        raise HTTPException(status_code=502, detail="charging.stop.failed")
    return {"status": "Accepted"}
