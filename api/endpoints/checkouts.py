import asyncio

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from db.init_db import (
    get_db,
    SessionLocal,
    Connector as ConnectorModel,
    Evse as EvseModel,
    Tariff as TariffModel,
    Location as LocationModel,
    Checkout as CheckoutModel,
)

from schemas.checkouts import Checkout, CheckoutCreate, CheckoutCreateResponse
from utils import live
from utils.utils import generate_pricing, stripe_account_kwargs

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

    db_checkout = CheckoutModel(connector_id=evse.connectors[0].id, tariff_id=tariff.id)
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
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)

    return CheckoutCreateResponse(
        id=db_checkout.id,
        url=checkout.url,
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
                "pricing": generate_pricing(db_checkout.id),
                "evse_status": db_evse.status if db_evse is not None else None,
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
                if snapshot.transaction_end_time is not None:
                    break
                try:
                    await asyncio.wait_for(
                        queue.get(), timeout=EVENTS_SNAPSHOT_SECONDS
                    )
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
    """Driver-requested remote stop of the checkout's running session.

    Sends RequestStopTransaction (1.6: RemoteStopTransaction) to the station.
    Settlement is untouched here: the charger's transaction-end event drives
    the normal capture path, same as unplugging."""
    db_checkout = db.query(CheckoutModel).filter(CheckoutModel.id == id).first()
    if db_checkout is None:
        raise HTTPException(status_code=404, detail="charging.error.sessionnotfound")
    if (
        db_checkout.transaction_start_time is None
        or db_checkout.transaction_end_time is not None
        or db_checkout.remote_request_transaction_id is None
    ):
        raise HTTPException(status_code=409, detail="charging.error.notrunning")

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
