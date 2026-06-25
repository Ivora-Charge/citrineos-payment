from io import BytesIO
from logging import error, info, warning
from typing import List, Tuple
from fastapi import FastAPI
import requests
import stripe
from sqlalchemy.orm import Session

from db.init_db import get_db, Checkout, Connector, Evse, Location, Operator
from utils.utils import generate_pricing, stripe_account_kwargs


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
        return

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
