from io import BytesIO
from logging import error, info, warning
from typing import List, Tuple
from fastapi import FastAPI
import requests
import stripe
from sqlalchemy.orm import Session

from config import Config
from db.init_db import get_db, Checkout, Connector, Evse, Location, Operator
from utils.utils import generate_pricing, stripe_account_kwargs

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
        db: Session = next(get_db())
        db_checkout = db.query(Checkout).filter(Checkout.id == checkout_id).first()
        if db_checkout is None:
            error(
                f" [integrations] CAPTURE ERROR - Could not find Checkout: {checkout_id}"
            )
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

        pricing = generate_pricing(checkout_id=checkout_id)

        # You can never capture more than was authorized (the manual-capture hold
        # placed at checkout). If the session's gross cost exceeds the
        # authorization, capture the full hold instead of letting Stripe reject
        # the capture for exceeding the authorized amount.
        amount_to_capture = pricing.total_costs_gross
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

        suc_intent = stripe.PaymentIntent.capture(
            intent=db_checkout.payment_intent_id,
            amount_to_capture=amount_to_capture,
            **stripe_account_kwargs(db_operator.stripe_account_id),
        )

        if suc_intent.status != "succeeded":
            error(
                f"CAPTURE ERROR - Could not capture the costs for Checkout: {db_checkout.id}"
            )
            return

        info(f"CAPTURE SUCCESS - Captured the costs for Checkout: {db_checkout.id}")

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

        try:
            overage_intent = stripe.PaymentIntent.create(
                amount=int(overage_subunits),
                currency=pricing.currency.lower(),
                customer=customer_id,
                payment_method=payment_method_id,
                off_session=True,
                confirm=True,
                metadata={"checkoutId": db_checkout.id, "type": "overage"},
                **stripe_account_kwargs(db_operator.stripe_account_id),
            )
            db_checkout.overage_payment_intent_id = overage_intent.id
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
