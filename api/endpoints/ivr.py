"""Pay-by-phone IVR (Twilio Programmable Voice + <Pay>).

Call flow: greeting -> caller keys the charger's phone code (printed on the
signage next to the toll-free number) -> price + hold read back -> Twilio
<Pay> collects the card via DTMF and tokenizes it straight into Stripe (the
digits never reach this service, keeping it out of PCI scope) -> we place the
same manual-capture hold the QR flow places -> the shared post-payment path
(webhooks.handle_web_portal) authorizes and fires RequestStartTransaction, and
settlement/capture ride the normal transaction-end machinery untouched.

Every endpoint returns TwiML and validates X-Twilio-Signature; with no
TWILIO_AUTH_TOKEN configured the whole feature fails closed (503).

State between webhook hops is carried in the action-URL query string (Twilio
signs URL + params, so it is tamper-evident). All action URLs are relative --
Twilio resolves them against the requested URL, so they work behind any host.
"""

from base64 import b64encode
from hashlib import sha1
from hmac import compare_digest, new as hmac_new
from logging import info, warning
from xml.sax.saxutils import escape, quoteattr

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from config import Config
from db.init_db import (
    get_db,
    Checkout as CheckoutModel,
    Connector as ConnectorModel,
    Evse as EvseModel,
    Location as LocationModel,
    Tariff as TariffModel,
)
from api.endpoints.webhooks import handle_web_portal
from utils.utils import stripe_account_kwargs

router = APIRouter()

MAX_CODE_ATTEMPTS = 3


# --- Twilio plumbing ---------------------------------------------------------


async def _validated_form(request: Request) -> dict:
    """Read the form body and enforce Twilio's signature scheme: HMAC-SHA1 of
    (full URL + POST params concatenated key+value in sorted key order) with
    the account auth token, base64, in X-Twilio-Signature."""
    if not Config.TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="pay-by-phone not configured")
    form = {k: v for k, v in (await request.form()).items()}

    url = str(request.url)
    if Config.IVR_PUBLIC_BASE_URL:
        url = Config.IVR_PUBLIC_BASE_URL.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
    payload = url + "".join(k + form[k] for k in sorted(form))
    expected = b64encode(
        hmac_new(
            Config.TWILIO_AUTH_TOKEN.encode(), payload.encode(), sha1
        ).digest()
    ).decode()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not compare_digest(expected, signature):
        warning(" [IVR] rejected request with bad Twilio signature")
        raise HTTPException(status_code=403, detail="bad signature")
    return form


def _twiml(*verbs: str) -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response>'
        + "".join(verbs)
        + "</Response>",
        media_type="text/xml",
    )


def _say(text: str) -> str:
    return f"<Say>{escape(text)}</Say>"


def _redirect(url: str) -> str:
    return f'<Redirect method="POST">{escape(url)}</Redirect>'


def _gather(action: str, num_digits: int | None, *inner: str) -> str:
    digits_attr = f' numDigits="{num_digits}"' if num_digits else ' finishOnKey="#"'
    return (
        f'<Gather input="dtmf" method="POST" timeout="10" '
        f"action={quoteattr(action)}{digits_attr}>" + "".join(inner) + "</Gather>"
    )


def _assistance_verbs() -> list[str]:
    if Config.IVR_SUPPORT_PHONE:
        return [
            _say("Connecting you to support."),
            f"<Dial>{escape(Config.IVR_SUPPORT_PHONE)}</Dial>",
        ]
    return [_say("Sorry, live assistance is not available on this line. Goodbye.")]


# --- Spoken text helpers -----------------------------------------------------

_CURRENCY_WORDS = {"usd": ("dollar", "cent"), "eur": ("euro", "cent")}


def _spoken_money(amount: float, currency: str) -> str:
    major_word, minor_word = _CURRENCY_WORDS.get(
        (currency or "").lower(), ((currency or "").upper(), None)
    )
    major = int(amount)
    minor = int(round((amount - major) * 100))
    if minor_word is None:
        return f"{amount:g} {major_word}"

    def plural(n: int, word: str) -> str:
        return f"{n} {word}" + ("s" if n != 1 else "")

    if major and minor:
        return f"{plural(major, major_word)} and {plural(minor, minor_word)}"
    if major:
        return plural(major, major_word)
    return plural(minor, minor_word)


def _spoken_pricing(tariff: TariffModel) -> str:
    parts = []
    if tariff.price_kwh:
        parts.append(
            f"{_spoken_money(tariff.price_kwh, tariff.currency)} per kilowatt hour"
        )
    if tariff.price_minute:
        parts.append(
            f"{_spoken_money(tariff.price_minute, tariff.currency)} per minute"
        )
    if tariff.price_session:
        parts.append(
            f"a session fee of {_spoken_money(tariff.price_session, tariff.currency)}"
        )
    if tariff.payment_fee:
        parts.append(f"a {tariff.payment_fee:g} percent transaction fee")
    return ", plus ".join(parts) if parts else "shown on the charger"


def _spoken_code(code: str) -> str:
    return " ".join(code)  # digit by digit


# --- Call flow ---------------------------------------------------------------


def _ask_for_code(attempt: int, *, greet: bool) -> Response:
    verbs = []
    if greet:
        # "Automated assistant" is the CA B.O.T. Act bot disclosure.
        verbs.append(
            _say(
                f"Welcome to {Config.IVR_NETWORK_NAME} pay by phone. "
                "I'm an automated assistant."
            )
        )
    prompt = (
        "Please enter the charger code printed on the charger, "
        "then press the pound key."
    )
    if Config.IVR_SUPPORT_PHONE:
        prompt += " Or press 0, then pound, for assistance."
    verbs.append(_gather(f"code?attempt={attempt}", None, _say(prompt)))
    # Reached only when <Gather> times out with no input.
    if attempt < MAX_CODE_ATTEMPTS:
        verbs.append(_say("Sorry, I didn't receive anything."))
        verbs.append(_redirect(f"voice?attempt={attempt + 1}"))
    else:
        verbs.append(_say("Sorry, we couldn't complete your request. Goodbye."))
    return _twiml(*verbs)


@router.post("/voice")
async def voice(request: Request):
    """Entry point (the number's Voice webhook) and retry target."""
    await _validated_form(request)
    attempt = _int_param(request, "attempt", 1)
    return _ask_for_code(attempt, greet=attempt == 1)


@router.post("/code")
async def code(request: Request, db: Session = Depends(get_db)):
    form = await _validated_form(request)
    attempt = _int_param(request, "attempt", 1)
    digits = (form.get("Digits") or "").strip()

    if digits == "0":
        return _twiml(*_assistance_verbs())

    evse = (
        db.query(EvseModel).filter(EvseModel.phone_code == digits).first()
        if digits
        else None
    )
    if evse is None:
        if attempt >= MAX_CODE_ATTEMPTS:
            return _twiml(
                _say("Sorry, we couldn't find that charger. Goodbye."),
            )
        return _twiml(
            _say("Sorry, there is no charger with that code."),
            _redirect(f"voice?attempt={attempt + 1}"),
        )

    chain = _catalog_chain(db, evse)
    if chain is None:
        return _twiml(
            _say(
                "Sorry, this charger cannot accept phone payments right now."
            ),
            *_assistance_verbs(),
        )
    connector, tariff, location = chain

    busy = (
        db.query(CheckoutModel)
        .filter(
            CheckoutModel.connector_id == connector.id,
            CheckoutModel.transaction_start_time.isnot(None),
            CheckoutModel.transaction_end_time.is_(None),
        )
        .first()
    )
    if busy is not None:
        return _twiml(
            _say(
                "That charger is currently in use. "
                "Please try another charger. Goodbye."
            )
        )

    where = ", ".join(p for p in [location.address, location.city] if p)
    confirm_prompt = (
        f"Charger {_spoken_code(evse.phone_code)}"
        + (f" at {where}. " if where else ". ")
        + f"The price is {_spoken_pricing(tariff)}. "
        f"A hold of {_spoken_money(tariff.authorization_amount, tariff.currency)} "
        "will be placed on your card, and you will only pay for what you use. "
        "Press 1 to pay and start charging. Press 2 to enter a different code."
    )
    return _twiml(
        _gather(f"confirm?evse={evse.id}", 1, _say(confirm_prompt)),
        _say("Sorry, I didn't receive anything. Goodbye."),
    )


@router.post("/confirm")
async def confirm(request: Request, db: Session = Depends(get_db)):
    form = await _validated_form(request)
    digit = (form.get("Digits") or "").strip()

    if digit == "2":
        return _ask_for_code(1, greet=False)
    if digit == "0":
        return _twiml(*_assistance_verbs())
    if digit != "1":
        return _twiml(
            _say("Sorry, that wasn't one of the options."),
            _redirect("voice?attempt=1"),
        )

    evse = (
        db.query(EvseModel)
        .filter(EvseModel.id == _int_param(request, "evse", 0))
        .first()
    )
    chain = _catalog_chain(db, evse) if evse is not None else None
    if chain is None:
        return _twiml(
            _say("Sorry, something went wrong."), *_assistance_verbs()
        )
    connector, tariff, location = chain

    db_checkout = CheckoutModel(
        connector_id=connector.id, tariff_id=tariff.id, source="ivr"
    )
    db.add(db_checkout)
    db.commit()
    db.refresh(db_checkout)

    # Tokenize only (no chargeAmount): the hold is placed server-side with
    # capture_method=manual so IVR checkouts settle exactly like QR ones.
    # Connector convention: operators on a real Connect account get a Twilio
    # Pay connector NAMED AFTER the acct_... id, so the token is created on
    # the same Stripe account the PaymentIntent will run on. Platform
    # operators use the default connector from config.
    account_id = location.operator.stripe_account_id
    connector_name = (
        account_id
        if account_id and account_id.startswith("acct_")
        else Config.TWILIO_PAY_CONNECTOR
    )
    connector_attr = (
        f" paymentConnector={quoteattr(connector_name)}" if connector_name else ""
    )
    pay = (
        f'<Pay tokenType="reusable" maxAttempts="2" '
        f"action={quoteattr(f'pay?checkout={db_checkout.id}')}{connector_attr}/>"
    )
    return _twiml(_say("Please have your card ready."), pay)


@router.post("/pay")
async def pay(request: Request, db: Session = Depends(get_db)):
    form = await _validated_form(request)
    result = form.get("Result")
    token = form.get("PaymentToken")

    if result == "caller-hung-up":
        return _twiml()
    db_checkout = (
        db.query(CheckoutModel)
        .filter(CheckoutModel.id == _int_param(request, "checkout", 0))
        .first()
    )
    if result != "success" or not token or db_checkout is None:
        info(" [IVR] payment not completed: result=%r", result)
        return _twiml(
            _say("Sorry, the payment could not be completed. Let's start over."),
            _redirect("voice?attempt=1"),
        )

    connector = (
        db.query(ConnectorModel)
        .filter(ConnectorModel.id == db_checkout.connector_id)
        .first()
    )
    evse = db.query(EvseModel).filter(EvseModel.id == connector.evse_id).first()
    chain = _catalog_chain(db, evse)
    if chain is None:
        return _twiml(_say("Sorry, something went wrong."), *_assistance_verbs())
    _, tariff, location = chain

    amount_cents = int(tariff.authorization_amount * 100)
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=tariff.currency.lower(),
            payment_method=token,
            confirm=True,
            capture_method="manual",
            description=f"Pay-by-phone hold, charger {evse.phone_code}",
            metadata={"checkoutId": db_checkout.id, "source": "ivr"},
            **stripe_account_kwargs(location.operator.stripe_account_id),
        )
    except stripe.error.CardError:
        return _twiml(
            _say("Sorry, your card was declined. Let's start over."),
            _redirect("voice?attempt=1"),
        )
    except stripe.error.StripeError:
        warning(" [IVR] Stripe error placing hold for checkout %s", db_checkout.id)
        return _twiml(
            _say("Sorry, the payment could not be processed."),
            *_assistance_verbs(),
        )
    if intent.status != "requires_capture":
        # 3DS/action-required can't be satisfied over DTMF; release and bail.
        warning(
            " [IVR] hold for checkout %s ended in status %s; canceling",
            db_checkout.id,
            intent.status,
        )
        try:
            stripe.PaymentIntent.cancel(
                intent.id,
                **stripe_account_kwargs(location.operator.stripe_account_id),
            )
        except stripe.error.StripeError:
            pass
        return _twiml(
            _say("Sorry, this card cannot be used over the phone."),
            *_assistance_verbs(),
        )

    db_checkout.authorization_amount = amount_cents

    # Same post-payment path as the QR/web flow: creates the OCPP
    # authorization, fires RequestStartTransaction, and releases the hold
    # itself if the start is rejected.
    try:
        await handle_web_portal(
            db, request.app.ocpp_integration, db_checkout, intent.id
        )
    except HTTPException:
        return _twiml(
            _say(
                "Sorry, we could not start the charger. "
                "The hold on your card has been released."
            ),
            *_assistance_verbs(),
        )

    db.refresh(db_checkout)
    if db_checkout.remote_request_status == "Accepted":
        return _twiml(
            _say(
                "Payment accepted. Please plug in the cable now if you "
                "haven't already. Charging will begin shortly. Goodbye."
            )
        )
    return _twiml(
        _say(
            "Sorry, the charger did not accept the start request. "
            "The hold on your card has been released."
        ),
        *_assistance_verbs(),
    )


# --- Lookups -----------------------------------------------------------------


def _int_param(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except ValueError:
        return default


def _catalog_chain(db: Session, evse: EvseModel):
    """Connector, tariff and location for an EVSE, or None when the catalog
    chain is incomplete (mirrors create_checkout's lookups)."""
    connector = (
        db.query(ConnectorModel).filter(ConnectorModel.evse_id == evse.id).first()
    )
    if connector is None:
        return None
    tariff = (
        db.query(TariffModel).filter(TariffModel.id == connector.tariff_id).first()
    )
    location = (
        db.query(LocationModel).filter(LocationModel.id == evse.location_id).first()
    )
    if tariff is None or location is None or location.operator is None:
        return None
    return connector, tariff, location
