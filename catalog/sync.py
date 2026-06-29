"""Reusable upsert for the payment catalog chain
(Operator -> Location -> Tariff -> EVSE -> Connector).

This is the single source of truth used by both the manual ``seed.py`` CLI and
the HTTP sync API (``api/endpoints/catalog.py``). Without these rows the handler
in ``integrations/citrineos/citrineos.py`` raises "EVSE not found" before it can
send the SetDisplayMessage (QR / payment link) to the station.

Idempotent: re-running upserts the existing rows (matched on their natural keys)
instead of creating duplicates. The caller owns the transaction -- this module
only ``flush()``es so the caller can ``commit()`` (or roll back) around it.
"""

from typing import Optional

from db.init_db import (
    Connector,
    Evse,
    Location,
    Operator,
    Tariff,
)

# Defaults mirror the historical seed.py values so partial sync payloads keep
# working exactly like the old manual seed.
DEFAULT_PRICE_KWH = 0.30
DEFAULT_PRICE_MINUTE = 0.0
DEFAULT_PRICE_SESSION = 0.0
DEFAULT_PAYMENT_FEE = 0.0
DEFAULT_TAX_RATE = 0.0
DEFAULT_AUTHORIZATION_AMOUNT = 25.0
DEFAULT_CURRENCY = "usd"
DEFAULT_POWER_TYPE = "AC_1_PHASE"
DEFAULT_MAX_VOLTAGE = 240
DEFAULT_MAX_AMPERAGE = 32


def get_or_create(db, model, defaults=None, **lookup):
    """Look up ``model`` by ``lookup``; create it (or update its ``defaults``)
    if missing. Flushes to assign the PK without committing."""
    obj = db.query(model).filter_by(**lookup).first()
    if obj is None:
        obj = model(**lookup, **(defaults or {}))
        db.add(obj)
        db.flush()  # assign PK without committing
        created = True
    else:
        for k, v in (defaults or {}).items():
            setattr(obj, k, v)
        created = False
    return obj, created


def upsert_payment_catalog(
    db,
    *,
    operator_name: str,
    stripe_account_id: str,
    location_id: str,
    address: str,
    postal_code: str,
    city: str,
    state: str,
    country: str,
    station_id: str,
    tenant_id: str,
    ocpp_evse_id: int,
    evse_id: str,
    connector_id: Optional[str] = None,
    currency: str = DEFAULT_CURRENCY,
    tax_rate: float = DEFAULT_TAX_RATE,
    authorization_amount: float = DEFAULT_AUTHORIZATION_AMOUNT,
    price_kwh: float = DEFAULT_PRICE_KWH,
    price_minute: float = DEFAULT_PRICE_MINUTE,
    price_session: float = DEFAULT_PRICE_SESSION,
    payment_fee: float = DEFAULT_PAYMENT_FEE,
    power_type: str = DEFAULT_POWER_TYPE,
    max_voltage: int = DEFAULT_MAX_VOLTAGE,
    max_amperage: int = DEFAULT_MAX_AMPERAGE,
) -> dict:
    """Upsert the full operator -> location -> tariff -> evse -> connector chain.

    Natural keys: operator.name, location.location_id, evse.evse_id,
    connector.connector_id. The caller is responsible for committing.

    Returns a dict with the resulting row ids and a ``created`` map indicating
    which rows were newly inserted on this call.
    """
    if connector_id is None:
        connector_id = f"{evse_id}-1"

    operator, operator_created = get_or_create(
        db,
        Operator,
        name=operator_name,
        defaults={"stripe_account_id": stripe_account_id},
    )

    location, location_created = get_or_create(
        db,
        Location,
        location_id=location_id,
        defaults={
            "operator_id": operator.id,
            "address": address,
            "postal_code": postal_code,
            "city": city,
            "state": state,
            "country": country,
        },
    )

    # Tariff idempotency: seed.py keyed the tariff on (currency, tax_rate), which
    # is fragile -- two stations with the same currency would share/clobber one
    # tariff. Instead reuse the tariff already wired to this connector (if any),
    # otherwise create a fresh one. stripe_price_id is normally left untouched
    # (the handler lazily creates the Stripe Price on first use and writes it
    # back), but it IS cleared below when the hold amount/currency changes.
    existing_connector = (
        db.query(Connector).filter_by(connector_id=connector_id).first()
    )
    tariff_values = {
        "currency": currency,
        "tax_rate": tax_rate,
        "authorization_amount": authorization_amount,
        "price_kwh": price_kwh,
        "price_minute": price_minute,
        "price_session": price_session,
        "payment_fee": payment_fee,
    }
    if existing_connector is not None and existing_connector.tariff_id is not None:
        tariff = (
            db.query(Tariff).filter_by(id=existing_connector.tariff_id).first()
        )
    else:
        tariff = None
    if tariff is None:
        tariff = Tariff(**tariff_values)
        db.add(tariff)
        db.flush()
        tariff_created = True
    else:
        # Stripe Prices are immutable, so a change to the hold amount or currency
        # makes the previously-created stripe_price_id stale: the scan-and-charge
        # PaymentLink would keep authorizing the old amount. Clear it so
        # process_transaction_started_scan_and_charge recreates the Price from the
        # new authorization_amount/currency on the next charge. (Currency is
        # compared case-insensitively because Stripe normalizes it to lowercase,
        # so "USD" vs "usd" is not a real change.)
        if tariff.authorization_amount != authorization_amount or (
            (tariff.currency or "").lower() != (currency or "").lower()
        ):
            tariff.stripe_price_id = None
        for k, v in tariff_values.items():
            setattr(tariff, k, v)
        tariff_created = False

    evse, evse_created = get_or_create(
        db,
        Evse,
        evse_id=evse_id,
        defaults={
            "ocpp_evse_id": ocpp_evse_id,
            "status": "Available",
            "station_id": station_id,
            "tenant_id": tenant_id,
            "location_id": location.id,
        },
    )

    connector, connector_created = get_or_create(
        db,
        Connector,
        connector_id=connector_id,
        defaults={
            "power_type": power_type,
            "max_voltage": max_voltage,
            "max_amperage": max_amperage,
            "evse_id": evse.id,
            "tariff_id": tariff.id,
        },
    )

    return {
        "operator_id": operator.id,
        "location_id": location.id,
        "tariff_id": tariff.id,
        "evse_id": evse.id,
        "connector_id": connector.id,
        "created": {
            "operator": operator_created,
            "location": location_created,
            "tariff": tariff_created,
            "evse": evse_created,
            "connector": connector_created,
        },
    }
