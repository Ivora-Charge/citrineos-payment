"""Itemized receipt email, sent to the driver after settlement.

California's EVFS rules (NIST HB 44 3.40 recorded representations) require a
complete receipt delivered directly to the customer -- including the physical
site location -- rather than a URL the driver has to type in. This email IS
that receipt: every price component the driver was shown pre-session (energy,
time, session fee, tax, payment fee) is itemized, and the total matches what
was captured on the card.
"""

import smtplib
from email.message import EmailMessage
from html import escape
from logging import error, info

import requests
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Checkout, Connector, Evse, Location, Operator, Tariff
from schemas.checkouts import Pricing

RESEND_API_URL = "https://api.resend.com/emails"


def receipt_email_enabled() -> bool:
    return bool(Config.SMTP_FROM and (Config.RESEND_API_KEY or Config.SMTP_HOST))


def _money(subunits: "int | None", currency: str) -> str:
    return f"{(subunits or 0) / 100:.2f} {currency.upper()}"


def _fmt_time(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z") if dt is not None else "-"


def build_receipt_items(
    db_checkout: Checkout, pricing: Pricing, tariff: Tariff
) -> "list[tuple[str, str]]":
    """(label, amount) rows of the cost table, in display order. Only
    components priced in the tariff appear, mirroring the pre-session
    disclosure on the checkout page / charger display / IVR."""
    cur = pricing.currency
    items = []
    if tariff.price_kwh:
        items.append(
            (
                f"Energy: {pricing.energy_consumption_kwh or 0:.2f} kWh "
                f"x {tariff.price_kwh:.2f} {cur.upper()}/kWh",
                _money(pricing.energy_costs, cur),
            )
        )
    if tariff.price_minute:
        items.append(
            (
                f"Time: {pricing.time_consumption_min or 0:.0f} min "
                f"x {tariff.price_minute:.2f} {cur.upper()}/min",
                _money(pricing.time_costs, cur),
            )
        )
    if tariff.price_session:
        items.append(("Session fee", _money(pricing.session_costs, cur)))
    items.append(("Subtotal (net)", _money(pricing.total_costs_net, cur)))
    if pricing.tax_rate:
        items.append(
            (f"Tax ({pricing.tax_rate:g}%)", _money(pricing.tax_costs, cur))
        )
    if pricing.payment_fee:
        items.append(
            (
                f"Transaction fee ({pricing.payment_fee:g}%)",
                _money(pricing.payment_costs_gross, cur),
            )
        )
    items.append(("Total", _money(pricing.total_due, cur)))
    return items


def _collected_subunits(db_checkout: Checkout) -> int:
    return (db_checkout.captured_amount or 0) + (db_checkout.overage_amount or 0)


def build_receipt_text(
    db_checkout: Checkout,
    pricing: Pricing,
    tariff: Tariff,
    evse: Evse,
    location: Location,
    operator: Operator,
) -> str:
    lines = [
        f"Charging receipt -- {operator.name}",
        "",
        f"Session: {db_checkout.id}",
        f"Charger: {evse.evse_id}",
        f"Location: {location.address}, {location.postal_code or ''} "
        f"{location.city or ''}"
        f"{', ' + location.state if location.state else ''}"
        f"{', ' + location.country if location.country else ''}",
        f"Start: {_fmt_time(db_checkout.transaction_start_time)}",
        f"End: {_fmt_time(db_checkout.transaction_end_time)}",
        f"Energy delivered: {db_checkout.transaction_kwh or 0:.2f} kWh",
        "",
    ]
    for label, amount in build_receipt_items(db_checkout, pricing, tariff):
        lines.append(f"{label}: {amount}")

    collected = _collected_subunits(db_checkout)
    if collected != pricing.total_due:
        # Capture was capped at the pre-auth hold (or the overage charge
        # didn't go through): what actually left the card differs from the
        # session total, and the receipt must say what was charged.
        lines.append(f"Amount charged to card: {_money(collected, pricing.currency)}")
    lines += [
        "",
        f"Payment reference: {db_checkout.payment_intent_id}",
        "",
        f"Thank you for charging with {operator.name}.",
    ]
    return "\n".join(lines)


def build_receipt_html(
    db_checkout: Checkout,
    pricing: Pricing,
    tariff: Tariff,
    evse: Evse,
    location: Location,
    operator: Operator,
) -> str:
    e = escape
    address = (
        f"{location.address or ''}, {location.postal_code or ''} "
        f"{location.city or ''}"
        f"{', ' + location.state if location.state else ''}"
        f"{', ' + location.country if location.country else ''}"
    )
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0'>{e(label)}</td>"
        f"<td style='padding:4px 0;text-align:right'>{e(amount)}</td></tr>"
        for label, amount in build_receipt_items(db_checkout, pricing, tariff)
    )
    collected = _collected_subunits(db_checkout)
    charged_row = (
        f"<tr><td style='padding:4px 12px 4px 0'><b>Amount charged to card</b></td>"
        f"<td style='padding:4px 0;text-align:right'><b>"
        f"{e(_money(collected, pricing.currency))}</b></td></tr>"
        if collected != pricing.total_due
        else ""
    )
    return f"""
<div style="font-family:sans-serif;max-width:480px">
  <h2 style="margin-bottom:4px">Charging receipt</h2>
  <p style="margin-top:0;color:#555">{e(operator.name)}</p>
  <p>
    Session: {db_checkout.id}<br/>
    Charger: {e(evse.evse_id)}<br/>
    Location: {e(address)}<br/>
    Start: {e(_fmt_time(db_checkout.transaction_start_time))}<br/>
    End: {e(_fmt_time(db_checkout.transaction_end_time))}<br/>
    Energy delivered: {db_checkout.transaction_kwh or 0:.2f} kWh
  </p>
  <table style="border-collapse:collapse">{rows}{charged_row}</table>
  <p style="color:#555">Payment reference: {e(db_checkout.payment_intent_id or '')}</p>
  <p>Thank you for charging with {e(operator.name)}.</p>
</div>
"""


def send_receipt_email(db: Session, db_checkout: Checkout, pricing: Pricing) -> None:
    """Send the itemized receipt for a settled checkout. Best-effort: any
    failure is logged and swallowed -- settlement already happened and must
    never be rolled back or retried because of the mail server."""
    if not receipt_email_enabled():
        return
    if not db_checkout.customer_email:
        info(
            f" [receipt_email] Checkout {db_checkout.id}: no customer email "
            "(non-web channel); skipping receipt email."
        )
        return

    try:
        db_connector = (
            db.query(Connector).filter(Connector.id == db_checkout.connector_id).first()
        )
        evse = (
            db.query(Evse).filter(Evse.id == db_connector.evse_id).first()
            if db_connector
            else None
        )
        location = (
            db.query(Location).filter(Location.id == evse.location_id).first()
            if evse
            else None
        )
        operator = (
            db.query(Operator).filter(Operator.id == location.operator_id).first()
            if location
            else None
        )
        tariff = db.query(Tariff).filter(Tariff.id == db_checkout.tariff_id).first()
        if None in (evse, location, operator, tariff):
            error(
                f" [receipt_email] Checkout {db_checkout.id}: incomplete catalog "
                "chain; cannot build receipt."
            )
            return

        subject = (
            f"Your charging receipt -- {evse.evse_id} -- "
            f"{_money(pricing.total_due, pricing.currency)}"
        )
        text = build_receipt_text(
            db_checkout, pricing, tariff, evse, location, operator
        )
        html = build_receipt_html(
            db_checkout, pricing, tariff, evse, location, operator
        )

        if Config.RESEND_API_KEY:
            response = requests.post(
                RESEND_API_URL,
                json={
                    "from": Config.SMTP_FROM,
                    "to": [db_checkout.customer_email],
                    "subject": subject,
                    "text": text,
                    "html": html,
                },
                headers={"Authorization": f"Bearer {Config.RESEND_API_KEY}"},
                timeout=15,
            )
            response.raise_for_status()
        else:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = Config.SMTP_FROM
            message["To"] = db_checkout.customer_email
            message.set_content(text)
            message.add_alternative(html, subtype="html")
            with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as smtp:
                if Config.SMTP_STARTTLS:
                    smtp.starttls()
                if Config.SMTP_USER:
                    smtp.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
                smtp.send_message(message)
        info(
            f" [receipt_email] Checkout {db_checkout.id}: receipt sent to "
            f"{db_checkout.customer_email}."
        )
    except Exception as e:
        error(f" [receipt_email] Checkout {db_checkout.id}: send failed -- {e}")
