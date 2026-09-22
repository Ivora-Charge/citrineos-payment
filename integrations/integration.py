from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
from logging import error, info, warning
from typing import List, Tuple
from fastapi import FastAPI
import requests
import stripe
from sqlalchemy.orm import Session

from utils.platform_fees import fee_kwargs
from config import Config
from db.init_db import get_db, Checkout, Connector, Evse, Location, Operator
from utils.receipt_email import send_receipt_email
from utils.utils import generate_pricing, stripe_account_kwargs
from utils.payment_lifecycle import (
    checkout_account,
    record_stripe_terminal,
    release_checkout,
    cancel_checkout,
)
from utils import live

# Stripe rejects charges below the per-currency minimum (~$0.50 for USD); skip
# overages smaller than this rather than fail the second charge.
STRIPE_MIN_CHARGE_SUBUNITS = 50


class OcppIntegration:
    def __init__(self) -> None:
        pass

    async def receive_events(self, app: FastAPI = None) -> None:
        print(" [OcppIntegration] Receiving events...ddddd")
        pass

    async def capture_payment_transaction(
        self, app: FastAPI = None, checkout_id: int = None
    ) -> None:
        """Capture the payment transaction for the given checkout_id."""
        with closing(next(get_db())) as db:
            db_checkout = (
                db.query(Checkout)
                .filter(Checkout.id == checkout_id)
                .with_for_update()
                .first()
            )
            if db_checkout is None:
                error(
                    f" [integrations] CAPTURE ERROR - Could not find Checkout: {checkout_id}"
                )
                return

            # Idempotency: a duplicate Ended event (or a reaper pass racing the real
            # end packet) must not capture twice. captured_at is our local marker;
            # the PaymentIntent status check below covers markers lost to a crash
            # between capture and commit.
            if db_checkout.captured_at is not None:
                info(
                    f" [integrations] Checkout {checkout_id} already captured at "
                    f"{db_checkout.captured_at}; skipping."
                )
                return

            if db_checkout.cancellation_requested_at is not None:
                release_checkout(db, checkout_id)
                return

            # Admin free charging (api/endpoints/checkouts.py start_free_checkout):
            # nothing was authorized, so there is nothing to capture. Mark it
            # settled so the page, the reaper and the stats treat it as closed.
            if db_checkout.payment_intent_id is None:
                info(
                    f" [integrations] Checkout {checkout_id}: free session "
                    f"(source={db_checkout.source!r}); settling without Stripe."
                )
                db_checkout.captured_at = datetime.now(timezone.utc)
                db_checkout.captured_amount = 0
                db.add(db_checkout)
                db.commit()
                return

            # Resolve the operator via the checkout's connector -> evse -> location ->
            # operator chain. The previous query cross-joined Operator without linking
            # Location.operator_id, so with more than one operator it returned an
            # arbitrary (wrong) stripe_account_id.
            db_operator: Operator = (
                db.query(Operator)
                .join(Location, Location.operator_id == Operator.id)
                .join(Evse, Evse.location_id == Location.id)
                .join(Connector, Connector.evse_id == Evse.id)
                .filter(Connector.id == db_checkout.connector_id)
                .first()
            )
            if db_operator is None:
                error(
                    f" [integrations] CAPTURE ERROR - Could not resolve operator for "
                    f"Checkout: {checkout_id}"
                )
                return

            account_id = checkout_account(db, db_checkout)
            pricing = generate_pricing(checkout_id=checkout_id, db=db)

            # You can never capture more than was authorized (the manual-capture hold
            # placed at checkout). If the session's total (gross + payment fee)
            # exceeds the authorization, capture the full hold instead of letting
            # Stripe reject the capture for exceeding the authorized amount.
            amount_to_capture = pricing.total_due
            overage_subunits = 0
            if (
                db_checkout.authorization_amount is not None
                and amount_to_capture > db_checkout.authorization_amount
            ):
                warning(
                    f" [integrations] Checkout {db_checkout.id}: gross cost "
                    f"{amount_to_capture} exceeds authorized "
                    f"{db_checkout.authorization_amount}; capping capture at the "
                    f"authorized amount."
                )
                overage_subunits = int(amount_to_capture) - int(
                    db_checkout.authorization_amount
                )
                amount_to_capture = int(db_checkout.authorization_amount)

            # Only a hold in requires_capture can be captured; anything else means
            # it was already settled or cancelled (e.g. by a previous run whose DB
            # marker didn't commit).
            intent = stripe.PaymentIntent.retrieve(
                db_checkout.payment_intent_id,
                **stripe_account_kwargs(account_id),
            )
            if intent.status != "requires_capture":
                warning(
                    f" [integrations] Checkout {db_checkout.id}: PaymentIntent "
                    f"{db_checkout.payment_intent_id} is '{intent.status}', not "
                    "'requires_capture'; marking settled without capturing."
                )
                if record_stripe_terminal(db_checkout, intent):
                    db.add(db_checkout)
                    db.commit()
                    live.notify(checkout_id)
                return

            # Stripe refuses captures below its minimum charge (amount_too_small,
            # ~$0.50): a zero- or micro-usage session (e.g. a remote start that hit
            # EVConnectTimeout without an EV) can never be captured. Cancel the
            # hold instead so the driver's money is released immediately rather
            # than lingering until the authorization expires after 7 days.
            if int(amount_to_capture) < STRIPE_MIN_CHARGE_SUBUNITS or (
                db_checkout.transaction_kwh == 0 and not db_checkout.power_active_import
            ):
                info(
                    f" [integrations] Checkout {db_checkout.id}: final amount "
                    f"{amount_to_capture} is below the Stripe minimum charge; "
                    "cancelling the hold instead of capturing."
                )
                db.commit()
                cancel_checkout(db, checkout_id, "no_billable_usage")
                return

            commission = fee_kwargs(
                amount_to_capture, db_checkout.platform_fee_bps, account_id
            )
            suc_intent = stripe.PaymentIntent.capture(
                intent=db_checkout.payment_intent_id,
                amount_to_capture=amount_to_capture,
                idempotency_key=f"checkout-{checkout_id}-capture",
                **commission,
                **stripe_account_kwargs(account_id),
            )

            if suc_intent.status != "succeeded":
                error(
                    f"CAPTURE ERROR - Could not capture the costs for Checkout: {db_checkout.id}"
                )
                return

            info(f"CAPTURE SUCCESS - Captured the costs for Checkout: {db_checkout.id}")
            db_checkout.captured_at = datetime.now(timezone.utc)
            db_checkout.captured_amount = int(amount_to_capture)
            db_checkout.platform_fee_amount = commission.get(
                "application_fee_amount", 0
            )
            db.add(db_checkout)
            db.commit()
            live.notify(checkout_id)

            # Overage: the hold was the ceiling on what the capture could collect, so
            # bill the remainder as a second off-session charge on the saved card.
            if (
                Config.OVERAGE_CHARGE_ENABLED
                and overage_subunits >= STRIPE_MIN_CHARGE_SUBUNITS
                and db_checkout.overage_payment_intent_id is None
            ):
                self._charge_overage(
                    db, db_checkout, db_operator, suc_intent, pricing, overage_subunits
                )
            # After the overage attempt, so "amount charged to card" on the
            # receipt reflects what was actually collected.
            send_receipt_email(db, db_checkout, pricing)
            return

    def _charge_overage(
        self,
        db: Session,
        db_checkout: Checkout,
        db_operator: Operator,
        hold_intent,
        pricing,
        overage_subunits: int,
    ) -> None:
        """Charge cost above the captured hold as a second off-session PaymentIntent
        on the saved card.

        The saved Customer + PaymentMethod live on the hold PaymentIntent, and are
        only present when the checkout saved the card (web-portal flow). The
        scan-and-charge PaymentLink flow doesn't save a card, so this no-ops and the
        session simply caps at the hold. Off-session declines (insufficient funds, or
        SCA required with the driver gone) are logged, not raised -- the guaranteed
        hold is already captured.
        """
        customer_id = getattr(hold_intent, "customer", None)
        payment_method_id = getattr(hold_intent, "payment_method", None)
        if not customer_id or not payment_method_id:
            info(
                f" [integrations] Checkout {db_checkout.id}: no saved card; skipping "
                f"${overage_subunits / 100:.2f} overage (capped at hold)."
            )
            return

        account_id = db_checkout.stripe_account_id or db_operator.stripe_account_id
        try:
            commission = fee_kwargs(
                overage_subunits,
                db_checkout.platform_fee_bps,
                account_id,
            )
            overage_intent = stripe.PaymentIntent.create(
                amount=int(overage_subunits),
                **commission,
                currency=pricing.currency.lower(),
                customer=customer_id,
                payment_method=payment_method_id,
                off_session=True,
                confirm=True,
                metadata={"checkoutId": db_checkout.id, "type": "overage"},
                **stripe_account_kwargs(account_id),
            )
            db_checkout.overage_payment_intent_id = overage_intent.id
            if overage_intent.status == "succeeded":
                db_checkout.overage_amount = int(overage_subunits)
                db_checkout.overage_platform_fee_amount = commission.get(
                    "application_fee_amount", 0
                )
            db.add(db_checkout)
            db.commit()
            info(
                f" [integrations] OVERAGE SUCCESS - Checkout {db_checkout.id}: charged "
                f"${overage_subunits / 100:.2f} (status={overage_intent.status})."
            )
        except stripe.error.CardError as e:
            error(
                f" [integrations] OVERAGE DECLINED - Checkout {db_checkout.id}: "
                f"${overage_subunits / 100:.2f} -- {getattr(e, 'user_message', None) or e}"
            )
        except Exception as e:
            error(
                f" [integrations] OVERAGE ERROR - Checkout {db_checkout.id}: "
                f"${overage_subunits / 100:.2f} -- {e}"
            )

    """
    Creates an Authorization in the CitrineOS system.
    
    Parameters:
        self: OcppIntegration - The OcppIntegration instance.
        transaction_id: str - The transaction ID.
        payment_intent_id: str - The payment intent ID.
        app: FastAPI - The FastAPI application.
    
    Returns:
        obj: an Authorization object or None if an error occurred.
    """

    async def create_authorization(
        self,
        idToken: str,
        idTokenType: str,
        additionalInfo: List[Tuple[str, str]],
        app: FastAPI = None,
        tenant_id: "str | int" = 1,
    ):
        pass

    def send_citrineos_message(
        self, station_id: str, tenant_id: str, url_path: str, json_payload: str
    ) -> requests.Response:
        pass


class FileIntegration:
    def __init__(self) -> None:
        pass

    """
    Uploads a file to FileIntegration.
    
    Parameters:
        self: FileIntegration - The FileIntegration instance.
        file: BytesIO - The file to upload.
        mime_type: str - The MIME type of the file.
        filename: str - The name of the file.
        filetitle: str - The title of the file.
    
    Returns:
        str - A url to the uploaded file.
    """

    def upload_file(
        self, file: BytesIO, mime_type: str, filename: str, filetitle: str
    ) -> str:
        pass
