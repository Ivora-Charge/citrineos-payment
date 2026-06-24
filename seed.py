"""Seed the payment service with the Operator -> Location -> Tariff -> EVSE ->
Connector chain that scan-and-charge needs. Without these rows the handler in
integrations/citrineos/citrineos.py raises "EVSE not found" before it can send
the SetDisplayMessage (QR / payment link) to the station.

Idempotent: re-running updates the existing rows (matched on their unique keys)
instead of creating duplicates.

Usage:
    cd citrineos-payment && source .venv/bin/activate
    python seed.py --station-id cp002 --tenant-id 1 \
                   --stripe-account-id acct_XXXXXXXXXXXX \
                   --currency usd --authorization-amount 25
"""

import argparse

from db.init_db import (
    SessionLocal,
    Operator,
    Location,
    Tariff,
    Evse,
    Connector,
)


def get_or_create(db, model, defaults=None, **lookup):
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--station-id", default="cp002",
                   help="OCPP station id (event header ocppConnectionName)")
    p.add_argument("--tenant-id", default="1")
    p.add_argument("--ocpp-evse-id", type=int, default=1,
                   help="OCPP evseId reported in TransactionEvent.evse.id")
    p.add_argument("--evse-id", default="cp002-1",
                   help="payment-service EVSE business key (unique)")
    p.add_argument("--location-id", default="loc-001")
    p.add_argument("--operator-name", default="Test Operator")
    p.add_argument("--stripe-account-id", required=True,
                   help="Stripe CONNECTED account id (acct_...). Must be real "
                        "for the payment-link/QR step to succeed.")
    p.add_argument("--currency", default="usd")
    p.add_argument("--authorization-amount", type=float, default=25.0)
    args = p.parse_args()

    db = SessionLocal()
    try:
        operator, _ = get_or_create(
            db, Operator,
            name=args.operator_name,
            defaults={"stripe_account_id": args.stripe_account_id},
        )

        location, _ = get_or_create(
            db, Location,
            location_id=args.location_id,
            defaults={
                "operator_id": operator.id,
                "address": "1 Test St",
                "postal_code": "00000",
                "city": "Testville",
                "state": "TS",
                "country": "USA",
            },
        )

        # Tariff: only the NOT NULL columns are required here. stripe_price_id is
        # left NULL on purpose -- the handler lazily creates the Stripe Price on
        # first use (citrineos.py:303-313) and writes the id back.
        tariff, _ = get_or_create(
            db, Tariff,
            currency=args.currency,
            tax_rate=0.0,
            defaults={
                "price_kwh": 0.30,
                "price_minute": 0.0,
                "price_session": 0.0,
                "authorization_amount": args.authorization_amount,
                "payment_fee": 0.0,
            },
        )

        evse, _ = get_or_create(
            db, Evse,
            evse_id=args.evse_id,
            defaults={
                "ocpp_evse_id": args.ocpp_evse_id,
                "status": "Available",
                "station_id": args.station_id,
                "tenant_id": args.tenant_id,
                "location_id": location.id,
            },
        )

        get_or_create(
            db, Connector,
            connector_id=f"{args.evse_id}-1",
            defaults={
                "power_type": "AC_1_PHASE",
                "max_voltage": 240,
                "max_amperage": 32,
                "evse_id": evse.id,
                "tariff_id": tariff.id,
            },
        )

        db.commit()
        print(f"Seeded station_id={args.station_id} tenant_id={args.tenant_id} "
              f"(operator={operator.id}, location={location.id}, "
              f"tariff={tariff.id}, evse={evse.id}).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
