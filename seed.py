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

from db.init_db import SessionLocal
from catalog.sync import upsert_payment_catalog


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
        # Delegate to the shared upsert used by the catalog sync API so the CLI
        # and the HTTP endpoint stay in lockstep. The address fields keep the
        # historical seed values; tariff price/fee fields use the module
        # defaults. stripe_price_id is left NULL on purpose -- the handler
        # lazily creates the Stripe Price on first use and writes the id back.
        result = upsert_payment_catalog(
            db,
            operator_name=args.operator_name,
            stripe_account_id=args.stripe_account_id,
            location_id=args.location_id,
            address="1 Test St",
            postal_code="00000",
            city="Testville",
            state="TS",
            country="USA",
            station_id=args.station_id,
            tenant_id=args.tenant_id,
            ocpp_evse_id=args.ocpp_evse_id,
            evse_id=args.evse_id,
            currency=args.currency,
            authorization_amount=args.authorization_amount,
        )

        db.commit()
        print(f"Seeded station_id={args.station_id} tenant_id={args.tenant_id} "
              f"(operator={result['operator_id']}, "
              f"location={result['location_id']}, "
              f"tariff={result['tariff_id']}, evse={result['evse_id']}).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
