import json
from uuid import uuid4
import stripe
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from logging import debug, exception
from json import loads
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Connector, Evse, Transaction, get_db, Checkout as CheckoutModel
from integrations.integration import OcppIntegration
from schemas.checkouts import RequestStartStopStatusEnumType

router = APIRouter()


def citrineos_call_succeeded(response) -> bool:
    """citrineos-core main returns a list of per-station confirmations from the
    message API where older versions returned a single object."""
    if response.status_code != 200:
        return False
    body = response.json()
    if isinstance(body, list):
        return any(item.get("success") for item in body if isinstance(item, dict))
    return bool(isinstance(body, dict) and body.get("success"))


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    STRIPE_SIGNATURE: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    event = None
    body = b""
    async for chunk in request.stream():
        body += chunk

    # charge.succeed is in connect_event_types, as we are using Stripe Standard accounts
    # for which the events are coming via the Connect-Webhook.
    # If we would use Stripe Express accounts, the events would be coming via the Account-Webhook
    account_event_types = []
    connect_event_types = ["checkout.session.completed"]
    try:
        event_type = loads(body.decode()).get("type")
        debug(" [*WEBHOOK*] Event type {}".format(event_type))
        if event_type in account_event_types:
            event = stripe.Webhook.construct_event(
                body, STRIPE_SIGNATURE, Config.STRIPE_ENDPOINT_SECRET_ACCOUNT
            )
        elif event_type in connect_event_types:
            event = stripe.Webhook.construct_event(
                body, STRIPE_SIGNATURE, Config.STRIPE_ENDPOINT_SECRET_CONNECT
            )
        else:
            debug(" [*WEBHOOK*] Unhandled event type {}".format(event_type))
            return {}
    except ValueError as e:
        raise e
    except stripe.error.SignatureVerificationError as e:
        raise e

    # Handle the event
    """
        Removed handling of account changes for now
    """
    if event.get("type") == "checkout.session.completed":
        # A Stripe Checkout session completed
        # Payment was successful, try to start a charging session
        checkout_session = event.get("data").get("object")
        paymentIntentId = checkout_session.get("payment_intent")

        metadata = checkout_session.get("metadata")
        checkoutId = metadata.get("checkoutId")
        stationId = metadata.get("stationId")
        transactionId = metadata.get("transactionId")
        debug(
            " [Stripe] stationId: %r, transactionId: %r, checkoutId: %r, paymentIntentId: %r",
            stationId,
            transactionId,
            checkoutId,
            paymentIntentId,
        )

        ocpp_integration: OcppIntegration = request.app.ocpp_integration
        db_checkout = (
            db.query(CheckoutModel).filter(CheckoutModel.id == checkoutId).first()
        )
        if db_checkout is None:
            raise HTTPException(
                status_code=404, detail="No checkout found for payment intent"
            )
        db_checkout.authorization_amount = checkout_session.get("amount_total")

        if paymentIntentId and checkoutId and not transactionId:
            await handle_web_portal(db, ocpp_integration, db_checkout, paymentIntentId)
        elif paymentIntentId and checkoutId and stationId and transactionId:
            await handle_scan_and_charge(
                db,
                ocpp_integration,
                db_checkout,
                paymentIntentId,
                stationId,
                transactionId,
            )
        else:
            raise HTTPException(status_code=404, detail="Metadata missing")

    return


async def handle_web_portal(
    db: Session,
    ocpp_integration: OcppIntegration,
    db_checkout: CheckoutModel,
    paymentIntentId: str,
):
    db_checkout.payment_intent_id = paymentIntentId
    db.add(db_checkout)
    db.commit()

    # TODO: Remove this part when CitrineOS is correctly saving the idToken from RemoteStartRequests.
    authorization = await ocpp_integration.create_authorization(
        f"{Config.OCPP_REMOTESTART_IDTAG_PREFIX}{db_checkout.id}",
        "Central",
        [
            (paymentIntentId, "PaymentIntentId"),
        ],
    )
    if authorization is None:
        debug(" [Stripe] Unable to create authorization for transaction")
        cancel_payment_intent(paymentIntentId)
        raise HTTPException(
            status_code=404, detail="Unable to create authorization for transaction"
        )

    idToken = authorization["idToken"]
    request_body = {"remoteStartId": db_checkout.id, "idToken": idToken}

    db_connector = (
        db.query(Connector).filter(Connector.id == db_checkout.connector_id).first()
    )
    if db_connector is None:
        debug(
            " [CitrineOS] Connector not found for remote start request: %r",
            db_checkout.id,
        )
        return RequestStartStopStatusEnumType.REJECTED

    db_evse = db.query(Evse).filter(Evse.id == db_connector.evse_id).first()
    if db_evse is None:
        debug(
            " [CitrineOS] EVSE not found for remote start request: %r", db_checkout.id
        )
        return RequestStartStopStatusEnumType.REJECTED

    request_body["evseId"] = db_evse.ocpp_evse_id

    debug(" [Stripe] remote start request: %r", json.dumps(request_body))

    citrineos_module = (
        "evdriver"  # TODO set up programatic way to resolve module from action
    )
    action = "requestStartTransaction"
    response = ocpp_integration.send_citrineos_message(
        station_id=db_evse.station_id,
        tenant_id=db_evse.tenant_id,
        url_path=f"{citrineos_module}/{action}",
        json_payload=request_body,
    )
    remote_start_stop = RequestStartStopStatusEnumType.REJECTED
    if citrineos_call_succeeded(response):
        remote_start_stop = RequestStartStopStatusEnumType.ACCEPTED
    db_checkout.remote_request_status = remote_start_stop

    db.add(db_checkout)
    db.commit()
    debug(
        " [Stripe] paymentIntentId: %r, checkoutId: %r, requestStartStatus: %r",
        db_checkout.payment_intent_id,
        db_checkout.id,
        db_checkout.remote_request_status,
    )


async def handle_scan_and_charge(
    db: Session,
    ocpp_integration: OcppIntegration,
    db_checkout: CheckoutModel,
    paymentIntentId: str,
    stationId: str,
    transactionId: str,
):
    # Resolve the EVSE this checkout belongs to from the checkout's connector. The
    # transactionId baked into the QR/payment link refers to the plug-in session
    # that generated the QR, which may already have ended by the time the driver
    # pays (pay-before-plug) -- so don't depend on it being live to find the EVSE.
    db_connector = (
        db.query(Connector).filter(Connector.id == db_checkout.connector_id).first()
    )
    db_evse = None
    if db_connector is not None:
        db_evse = db.query(Evse).filter(Evse.id == db_connector.evse_id).first()
    if db_evse is None:
        db_evse = db.query(Evse).filter(Evse.station_id == stationId).first()
    if db_evse is None:
        debug(" [Stripe] No EVSE found for scan & charge checkout")
        cancel_payment_intent(paymentIntentId)
        raise HTTPException(
            status_code=404, detail="No EVSE found for scan & charge checkout"
        )

    ocppTransaction = (
        db.query(Transaction)
        .filter(
            Transaction.stationId == stationId,
            Transaction.transactionId == transactionId,
        )
        .first()
    )
    session_active = ocppTransaction is not None and ocppTransaction.isActive

    # Authorize the (current or upcoming) session and tie it to this payment.
    authorization = await ocpp_integration.create_authorization(
        str(uuid4()),
        "Central",
        [
            (transactionId, "TransactionId"),
            (paymentIntentId, "PaymentIntentId"),
        ],
    )
    if authorization is None:
        debug(" [Stripe] Unable to create authorization for transaction")
        cancel_payment_intent(paymentIntentId)
        raise HTTPException(
            status_code=404, detail="Unable to create authorization for transaction"
        )

    idToken = authorization["idToken"]
    request_body = {
        "remoteStartId": db_checkout.id,
        "idToken": idToken,
        "evseId": ocppTransaction.evse.id
        if session_active and ocppTransaction.evse is not None
        else db_evse.ocpp_evse_id,
    }

    # Send the remote start either way: if the cable is already plugged in
    # (post-plug scan & charge) the station starts charging immediately; if it
    # isn't yet (pay-before-plug) the station arms and starts the moment the
    # driver plugs in. Both paths echo remoteStartId back on the resulting
    # TransactionEvent(Started), which links the session to this checkout so the
    # PayServe charging page leaves the "waiting" state. Previously this handler
    # required a live transaction and cancelled the payment otherwise, breaking
    # the pay-before-plug flow entirely.
    debug(" [Stripe] remote start request: %r", json.dumps(request_body))
    response = ocpp_integration.send_citrineos_message(
        station_id=stationId,
        tenant_id=db_evse.tenant_id,
        url_path="evdriver/requestStartTransaction",
        json_payload=request_body,
    )
    remote_start_stop = RequestStartStopStatusEnumType.REJECTED
    if citrineos_call_succeeded(response):
        remote_start_stop = RequestStartStopStatusEnumType.ACCEPTED
    db_checkout.remote_request_status = remote_start_stop
    db_checkout.payment_intent_id = paymentIntentId
    db.add(db_checkout)
    db.commit()

    ocpp_integration.send_citrineos_message(
        station_id=stationId,
        tenant_id=db_evse.tenant_id,
        url_path="configuration/clearDisplayMessage",
        json_payload={"id": db_checkout.qr_code_message_id},
    )


def cancel_payment_intent(paymentIntendId: str):
    try:
        stripe.PaymentIntent.cancel(paymentIntendId)
    except Exception as e:
        exception(" [Stripe] Error while canceling payment intent: %r", e.__str__())
