"""One-off backfill: populate captured_at / captured_amount / overage_amount
on historical checkouts from Stripe (the source of truth for what was
actually collected).

New settlements record these at capture time; this script only exists so the
revenue dashboards include sessions settled before the columns existed. Safe
to re-run: rows that already have captured_amount are skipped.

Run from the repo root:  ./.venv/bin/python scripts/backfill_captured_amounts.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stripe
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Checkout, Connector, Evse, Location, Operator, get_db
from utils.utils import stripe_account_kwargs

stripe.api_key = Config.STRIPE_API_KEY
# Fail fast: a hung/retrying Stripe call must not stall the whole run (or hold
# the DB transaction open -- see the per-row commit below).
stripe.max_network_retries = 0


def operator_for(db: Session, checkout: Checkout) -> Operator | None:
    return (
        db.query(Operator)
        .join(Location, Location.operator_id == Operator.id)
        .join(Evse, Evse.location_id == Location.id)
        .join(Connector, Connector.evse_id == Evse.id)
        .filter(Connector.id == checkout.connector_id)
        .first()
    )


def main() -> None:
    # No init_db() here: the columns are added by the service's own startup,
    # and running DDL while the service holds idle-in-transaction connections
    # deadlocks the whole payment schema (learned the hard way).
    db: Session = next(get_db())
    candidates = (
        db.query(Checkout)
        .filter(
            Checkout.payment_intent_id.isnot(None),
            Checkout.captured_amount.is_(None),
        )
        .all()
    )
    print(f"{len(candidates)} checkout(s) to inspect")
    updated = 0
    for c in candidates:
        op = operator_for(db, c)
        kwargs = stripe_account_kwargs(op.stripe_account_id) if op else {}
        try:
            pi = stripe.PaymentIntent.retrieve(c.payment_intent_id, **kwargs)
        except stripe.error.InvalidRequestError:
            # The operator's stripe_account_id may have changed since this
            # checkout was created (e.g. dev 'platform' -> a real Connect
            # account); an intent is only visible in the account it was
            # created on, so fall back to the platform account.
            try:
                pi = stripe.PaymentIntent.retrieve(c.payment_intent_id)
                kwargs = {}
            except Exception as e:  # noqa: BLE001
                print(f"  checkout {c.id}: cannot retrieve {c.payment_intent_id}: {e}")
                continue
        except Exception as e:  # noqa: BLE001
            print(f"  checkout {c.id}: cannot retrieve {c.payment_intent_id}: {e}")
            continue
        if pi.status != "succeeded" or not pi.get("amount_received"):
            print(f"  checkout {c.id}: PI status {pi.status}; skipping")
            continue
        c.captured_amount = int(pi["amount_received"])
        if c.captured_at is None:
            # best effort: Stripe's created is the auth time; close enough for
            # bucketing pre-column history
            from datetime import datetime, timezone

            c.captured_at = datetime.fromtimestamp(pi["created"], tz=timezone.utc)
        if c.overage_payment_intent_id and c.overage_amount is None:
            try:
                opi = stripe.PaymentIntent.retrieve(c.overage_payment_intent_id, **kwargs)
                if opi.status == "succeeded":
                    c.overage_amount = int(opi["amount_received"])
            except Exception as e:  # noqa: BLE001
                print(f"  checkout {c.id}: overage retrieve failed: {e}")
        db.add(c)
        # Commit per row: keeps the transaction (and its row locks) short so a
        # slow Stripe call can't block concurrent DDL/queries, and preserves
        # progress if the run is interrupted.
        db.commit()
        updated += 1
        print(
            f"  checkout {c.id}: captured {c.captured_amount} "
            f"overage {c.overage_amount or 0} ({c.captured_at:%Y-%m-%d})",
            flush=True,
        )
    print(f"backfilled {updated} checkout(s)")


if __name__ == "__main__":
    main()
