from enum import Enum
import json
from urllib.parse import quote
from typing import List, Tuple
import asyncio
from aio_pika import connect_robust
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import ValidationError
import requests
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
import stripe

from config import Config
from logging import debug, exception, info, warning
from db.init_db import (
    MessageInfo as MessageInfoModel,
    get_db,
    Checkout as CheckoutModel,
    Connector as ConnectorModel,
    Evse as EvseModel,
    Location as LocationModel,
    Tariff as TariffModel,
)

from integrations.integration import FileIntegration, OcppIntegration
from integrations.charger_display import (
    DEFAULT_ADAPTER,
    display_adapter_for_vendor,
    get_display_adapter,
)
from schemas.status_notification import StatusNotificationRequest
from utils.utils import stripe_account_kwargs
from schemas.transaction_event import (
    MeasurandEnumType,
    TransactionEventEnumType,
    TriggerReasonEnumType,
    TransactionEventRequest,
)


class CitrineOsEventAction(str, Enum):
    TRANSACTIONEVENT = "TransactionEvent"
    STATUSNOTIFICATION = "StatusNotification"
    BOOTNOTIFICATION = "BootNotification"


class CitrineOSevent(BaseModel):
    action: CitrineOsEventAction
    payload: dict


class CitrineOSeventHeaders(BaseModel):
    # citrineos-core main renamed the event header stationId → ocppConnectionName
    stationId: str = Field(alias="ocppConnectionName")
    model_config = ConfigDict(populate_by_name=True)


class CitrineOSIntegration(OcppIntegration):
    def __init__(self, fileIntegration: FileIntegration):
        self.fileIntegration = fileIntegration

    async def create_authorization(
        self,
        idToken: str,
        idTokenType: str,
        additionalInfo: List[Tuple[str, str]],
        app: FastAPI = None,
    ):
        idToken = {
            "idToken": idToken,
            "type": idTokenType,
            "additionalInfo": [
                {"additionalIdToken": item[0], "type": item[1]}
                for item in additionalInfo
            ],
        }
        request_body = {
            "idToken": idToken,
            "idTokenInfo": {
                "status": "Accepted",
            },
        }
        # citrineos-core main no longer exposes PUT /data/evdriver/authorization;
        # authorizations are managed in the database (as the operator UI does),
        # so insert the row directly.
        try:
            db: Session = next(get_db())
            db.execute(
                sql_text(
                    'INSERT INTO "Authorizations" '
                    '("idToken", "idTokenType", "status", "tenantId", '
                    '"additionalInfo", "createdAt", "updatedAt") '
                    "VALUES (:id_token, :id_token_type, 'Accepted', 1, "
                    "CAST(:additional_info AS jsonb), now(), now()) "
                    'ON CONFLICT ("tenantId", "idToken", "idTokenType") DO NOTHING'
                ),
                {
                    "id_token": idToken["idToken"],
                    "id_token_type": idToken["type"],
                    "additional_info": json.dumps(idToken["additionalInfo"]),
                },
            )
            db.commit()
            return request_body
        except Exception as e:
            exception(" [CitrineOS] Error while creating authorization: %r", e)
            return

    def send_citrineos_message(
        self, station_id: str, tenant_id: str, url_path: str, json_payload: str
    ) -> requests.Response:
        request_url = (
            f"{Config.CITRINEOS_MESSAGE_API_URL}/{url_path}"
            f"?identifier={station_id}"
            f"&tenantId={tenant_id}"
        )

        return requests.post(request_url, json=json_payload)

    async def receive_events(self, app: FastAPI = None) -> None:
        # Outer reconnect loop: a broker restart (or any connection drop) must
        # not permanently kill consumption. connect_robust re-establishes the
        # connection/channel/declarations transparently; the loop additionally
        # guards against the consume task exiting on an unexpected error.
        backoff = 1
        while True:
            print(" [CitrineOS] Receiving events...")
            try:
                await self._consume_events()
                # _consume_events only returns when the iterator ends (e.g. the
                # connection closed) — loop and reconnect.
                warning(" [CitrineOS] Event consumer stopped, reconnecting...")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                exception(
                    " [CitrineOS] Event consumer crashed, reconnecting in %ss: %r",
                    backoff,
                    e.__str__(),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            await asyncio.sleep(1)

    async def _consume_events(self) -> None:
        # Perform connection
        connection = await connect_robust(
            ssl=Config.MESSAGE_BROKER_SSL_ACTIVE,
            host=Config.MESSAGE_BROKER_HOST,
            port=Config.MESSAGE_BROKER_PORT,
            login=Config.MESSAGE_BROKER_USER,
            password=Config.MESSAGE_BROKER_PASSWORD,
            virtualhost=Config.MESSAGE_BROKER_VHOST,
        )

        async with connection:
            # Creating a channel
            channel = await connection.channel()
            exchange: AbstractExchange = await channel.declare_exchange(
                name=Config.MESSAGE_BROKER_EXCHANGE_NAME,
                type=Config.MESSAGE_BROKER_EXCHANGE_TYPE,
            )

            # Declaring queue
            queue = await channel.declare_queue(
                Config.MESSAGE_BROKER_EVENT_CONSUMER_QUEUE_NAME, durable=True
            )

            # Bind headers
            arguments_list = [
                {
                    "action": "TransactionEvent",
                    "state": "1",
                    "x-match": "all",
                },
                {
                    "action": "StatusNotification",
                    "state": "1",
                    "x-match": "all",
                },
                # A rebooted charger has a blank screen but our display
                # debounce marker (display_message_id) survives in the DB, so
                # without hearing boots the standing QR would never be
                # re-sent after a power cycle.
                {
                    "action": "BootNotification",
                    "state": "1",
                    "x-match": "all",
                },
            ]
            for arguments in arguments_list:
                await queue.bind(
                    exchange=exchange,
                    routing_key="",
                    arguments=arguments,
                )

            info(
                " [CitrineOS] Awaiting events with keys: %r ",
                arguments_list.__str__(),
            )

            # Start listening the queue
            async with queue.iterator() as qiterator:
                message: AbstractIncomingMessage
                async for message in qiterator:
                    try:
                        async with (
                            message.process()
                        ):  # Processor acknowledges messages implicitly
                            debug(
                                f" [CitrineOS] event_message({message.headers.__str__()})"
                            )
                            await self.process_incoming_event(
                                event_message=message, exchange=exchange
                            )
                            debug(
                                " [CitrineOS] Event processed successfully: %r",
                                message.headers.__str__(),
                            )
                    except Exception:
                        exception(
                            " [CitrineOS] Processing error for message %r", message
                        )

    async def process_incoming_event(
        self, event_message: AbstractIncomingMessage, exchange: AbstractExchange
    ) -> None:
        try:
            decoded_body = event_message.body.decode()
            citrine_os_event = CitrineOSevent(**json.loads(decoded_body))

            if citrine_os_event.action == CitrineOsEventAction.TRANSACTIONEVENT:
                citrine_os_event_headers = CitrineOSeventHeaders(
                    **event_message.headers
                )
                transaction_event = TransactionEventRequest(**citrine_os_event.payload)
                if (
                    transaction_event.eventType == TransactionEventEnumType.Started
                    or transaction_event.triggerReason
                    == TriggerReasonEnumType.RemoteStart
                ):
                    await self.process_transaction_started(
                        transaction_event=transaction_event,
                        citrine_os_event_headers=citrine_os_event_headers,
                    )
                elif transaction_event.eventType == TransactionEventEnumType.Updated:
                    await self.process_transaction_updated(
                        transaction_event=transaction_event,
                    )
                elif transaction_event.eventType == TransactionEventEnumType.Ended:
                    await self.process_transaction_ended(
                        transaction_event=transaction_event,
                    )
                return
            elif citrine_os_event.action == CitrineOsEventAction.STATUSNOTIFICATION:
                citrine_os_event_headers = CitrineOSeventHeaders(
                    **event_message.headers
                )
                status_notification = StatusNotificationRequest(
                    **citrine_os_event.payload
                )
                await self.process_status_notification(
                    status_notification=status_notification,
                    citrine_os_event_headers=citrine_os_event_headers,
                )
            elif citrine_os_event.action == CitrineOsEventAction.BOOTNOTIFICATION:
                citrine_os_event_headers = CitrineOSeventHeaders(
                    **event_message.headers
                )
                await self.process_boot_notification(
                    citrine_os_event_headers=citrine_os_event_headers,
                )
        except ValidationError as e:
            if e.title == CitrineOSevent.__name__:
                debug(
                    " [CitrineOS] Received event which is not valid CitrineOS event: %r",
                    e.errors(),
                )
            elif e.title == TransactionEventRequest.__name__:
                warning(
                    " [CitrineOS] Received valid TransactionEvent, but fields missing: %r",
                    e.errors(),
                )
            else:
                # e.g. an OCPP 1.6 charger's StatusNotification (status/
                # connectorId instead of connectorStatus/evseId). Skip the
                # event instead of re-raising: an unparseable message must
                # never bounce the consumer loop and stall ALL stations.
                warning(
                    " [CitrineOS] Skipping event with unsupported payload shape"
                    " (OCPP 1.6 charger?): %r",
                    e.errors(),
                )
        except Exception as e:
            exception(" [CitrineOS] Processing error for incoming event: %r", e.__str__)
            raise e

    async def process_transaction_started(
        self,
        transaction_event: TransactionEventRequest,
        citrine_os_event_headers: CitrineOSeventHeaders,
    ) -> None:
        triggerReasonNoAuthArray = [
            TriggerReasonEnumType.CablePluggedIn,
            TriggerReasonEnumType.SignedDataReceived,
            TriggerReasonEnumType.EVDetected,
        ]
        if (
            Config.CITRINEOS_SCAN_AND_CHARGE
            and transaction_event.triggerReason in triggerReasonNoAuthArray
            and transaction_event.idToken is None
        ):
            await self.process_transaction_started_scan_and_charge(
                transaction_event=transaction_event,
                citrine_os_event_headers=citrine_os_event_headers,
            )
        else:
            await self.process_transaction_started_remote(
                transaction_event=transaction_event
            )

    async def process_transaction_started_scan_and_charge(
        self,
        transaction_event: TransactionEventRequest,
        citrine_os_event_headers: CitrineOSeventHeaders,
    ) -> None:
        transactionId = transaction_event.transactionInfo.transactionId
        stationId = citrine_os_event_headers.stationId

        db: Session = next(get_db())
        # If pricing is found to vary by evse, we need to change triggerReasonNoAuthArray to mandate events that know the evse
        # Then add a filter below, EvseModel.ocpp_evse_id == transaction_event.evse.id
        evse = db.query(EvseModel).filter(EvseModel.station_id == stationId).first()
        if evse is None:
            raise Exception("EVSE not found")

        # Prepayment policy: charging must never run before payment. Chargers
        # are provisioned with TxStartPoint=Authorized so this handler should
        # not fire at all; if a mis-provisioned charger starts an unauthorized
        # session anyway, stop it right away. The driver pays via the standing
        # QR, and the paid RequestStartTransaction starts the real session.
        if Config.SCAN_AND_CHARGE_REQUIRE_PREPAYMENT:
            warning(
                f" [CitrineOS] Unauthorized session {transactionId} on "
                f"{stationId}: prepayment required -- stopping it. Check the "
                "charger's TxStartPoint provisioning."
            )
            self.send_citrineos_message(
                station_id=stationId,
                tenant_id=evse.tenant_id,
                url_path="evdriver/requestStopTransaction",
                json_payload={"transactionId": transactionId},
            )
            return

        tariff = (
            db.query(TariffModel)
            .filter(TariffModel.id == evse.connectors[0].tariff_id)
            .first()
        )
        if tariff is None:
            raise Exception("No Tariff for EVSE found")

        location = (
            db.query(LocationModel).filter(LocationModel.id == evse.location_id).first()
        )
        if location is None:
            raise Exception("No Location for EVSE found")

        db_checkout = CheckoutModel(
            connector_id=evse.connectors[0].id, tariff_id=tariff.id
        )
        db_checkout = self.update_checkout_with_meter_values(
            transaction_event=transaction_event, db_checkout=db_checkout
        )
        db.add(db_checkout)
        db.commit()
        db.refresh(db_checkout)

        stripe_account_id = location.operator.stripe_account_id
        self._ensure_stripe_price(db, tariff, stripe_account_id)

        try:
            payment_link_url = await self.create_payment_link(
                stripe_price_id=tariff.stripe_price_id,
                stripe_account_id=stripe_account_id,
                stationId=stationId,
                evseId=evse.evse_id,
                transactionId=transactionId,
                checkoutId=db_checkout.id,
            )
        except stripe.error.InvalidRequestError as e:
            # Stripe Prices are account-scoped: a cached stripe_price_id goes
            # stale when the operator's account changes (dev 'platform' -> a
            # real Connect account, or a charger claimed into another tenant).
            # Heal by recreating the Price on the current account and retrying
            # once instead of dropping the whole checkout/QR.
            if "No such price" not in str(e):
                raise
            warning(
                f" [CitrineOS] Tariff {tariff.id}: price "
                f"{tariff.stripe_price_id} not found on account "
                f"{stripe_account_id!r}; recreating."
            )
            tariff.stripe_price_id = None
            db.add(tariff)
            db.commit()
            self._ensure_stripe_price(db, tariff, stripe_account_id)
            payment_link_url = await self.create_payment_link(
                stripe_price_id=tariff.stripe_price_id,
                stripe_account_id=stripe_account_id,
                stationId=stationId,
                evseId=evse.evse_id,
                transactionId=transactionId,
                checkoutId=db_checkout.id,
            )

        # Point the QR at the PayServe charger-info page (carrying the Stripe
        # payment link in the `pay` query param) so the driver sees charger/tariff
        # details and taps "Pay now" before being sent to the Stripe checkout. How
        # the QR is delivered depends on the charger family (image
        # SetDisplayMessage vs a Renova DataTransfer) -- routed via the adapter.
        checkout_page_url = (
            f"{Config.CLIENT_URL}/checkout/{evse.evse_id}"
            f"?pay={quote(payment_link_url, safe='')}"
        )
        self._resolve_display_adapter_type(db, evse)
        adapter = get_display_adapter(evse)
        db_checkout.qr_code_message_id = await adapter.show_transaction_qr(
            self,
            db,
            evse,
            payment_url=checkout_page_url,
            transaction_id=transactionId,
            price=tariff.price_kwh,
            currency=tariff.currency,
        )
        db.add(db_checkout)
        db.commit()

    def _ensure_stripe_price(self, db: Session, tariff, stripe_account_id) -> None:
        """Create the tariff's hold Price on the *operator's* Stripe account if
        it doesn't exist yet. Prices are account-scoped, so this must live on
        the same account the PaymentLink is created on -- creating it on the
        platform account while charging on a connected account yields
        'No such price'."""
        if tariff.stripe_price_id is not None:
            return
        price = stripe.Price.create(
            currency=tariff.currency.lower(),
            metadata={"tariffId": tariff.id},
            product_data={"name": "Charging Session Authorization Amount"},
            tax_behavior="inclusive",
            unit_amount=int(tariff.authorization_amount * 100),
            **stripe_account_kwargs(stripe_account_id),
        )
        tariff.stripe_price_id = price.id
        db.add(tariff)
        db.commit()

    async def create_payment_link(
        self,
        stripe_price_id: str,
        stripe_account_id: str,
        stationId: str,
        evseId: str,
        transactionId: str,
        checkoutId: int,
    ) -> str:
        transactionPaymentLink = stripe.PaymentLink.create(
            after_completion={
                "redirect": {
                    "url": f"{Config.CLIENT_URL}/charging/{evseId}/{checkoutId}"
                },
                "type": "redirect",
            },
            line_items=[
                {
                    "price": stripe_price_id,
                    "quantity": 1,
                }
            ],
            metadata={
                "stationId": stationId,
                "transactionId": transactionId,
                "checkoutId": checkoutId,
            },
            # Save the card (Stripe vaults it under a Customer; we keep only the
            # tokens on the PaymentIntent) so settlement can bill any cost above
            # the hold as a second off-session "overage" charge -- same as the
            # web-portal checkout. Without this, scan & charge sessions can't be
            # overage-billed and just cap at the hold.
            customer_creation="always",
            payment_intent_data={
                "capture_method": "manual",
                "setup_future_usage": "off_session",
            },
            payment_method_types=["card"],
            # No completed_sessions limit: that restriction deactivates the
            # PaymentLink after the first completed payment, so re-scanning the
            # QR / tapping "Pay now" again hit a dead link. Keep it always active.
            **stripe_account_kwargs(stripe_account_id),
        )
        return transactionPaymentLink.url

    async def process_transaction_started_remote(
        self, transaction_event: TransactionEventRequest
    ) -> None:
        db: Session = next(get_db())
        db_checkout = (
            db.query(CheckoutModel)
            .filter(CheckoutModel.id == transaction_event.transactionInfo.remoteStartId)
            .first()
        )
        if db_checkout is None:
            info(
                " [CitrineOS] Checkout not found for transaction start event: %r",
                transaction_event,
            )
            return

        db_checkout.transaction_start_time = transaction_event.timestamp
        db_checkout.remote_request_transaction_id = (
            transaction_event.transactionInfo.transactionId
        )
        db_checkout = self.update_checkout_with_meter_values(
            transaction_event=transaction_event, db_checkout=db_checkout
        )
        db.add(db_checkout)
        db.commit()
        db.refresh(db_checkout)

        # The paid session is running: take the standing "scan to pay" QR down
        # so nobody scans/pays for a connector that is already charging. It is
        # re-pushed when the connector returns to Available/Occupied.
        try:
            db_connector = (
                db.query(ConnectorModel)
                .filter(ConnectorModel.id == db_checkout.connector_id)
                .first()
            )
            db_evse = (
                db.query(EvseModel).filter(EvseModel.id == db_connector.evse_id).first()
                if db_connector
                else None
            )
            if db_evse is not None and db_evse.display_message_id is not None:
                await self.clear_standing_qr(db, db_evse)
        except Exception as e:
            exception(" [CitrineOS] Standing-QR clear failed: %r", e.__str__())
        return

    def find_checkout_for_event(
        self, db: Session, transaction_event: TransactionEventRequest
    ) -> "CheckoutModel | None":
        """Locate the Checkout a TransactionEvent belongs to.

        CitrineOS only echoes transactionInfo.remoteStartId on the *Started*
        event; Updated/Ended events carry remoteStartId=None. The Started handler
        therefore records remote_request_transaction_id = transactionId, and we
        match subsequent events by that transaction id. remoteStartId is still
        tried first (and as a fallback) so both paths keep working.
        """
        tx_info = transaction_event.transactionInfo
        db_checkout = None
        if tx_info.remoteStartId is not None:
            db_checkout = (
                db.query(CheckoutModel)
                .filter(CheckoutModel.id == tx_info.remoteStartId)
                .first()
            )
        if db_checkout is None and tx_info.transactionId is not None:
            db_checkout = (
                db.query(CheckoutModel)
                .filter(
                    CheckoutModel.remote_request_transaction_id
                    == tx_info.transactionId
                )
                .first()
            )
        return db_checkout

    async def process_transaction_updated(
        self, transaction_event: TransactionEventRequest
    ) -> None:
        db: Session = next(get_db())
        db_checkout = self.find_checkout_for_event(db, transaction_event)
        if db_checkout is None:
            info(
                " [CitrineOS] Checkout not found for transaction update event: %r",
                transaction_event,
            )
            return

        db_checkout = self.update_checkout_with_meter_values(
            transaction_event=transaction_event, db_checkout=db_checkout
        )
        db.add(db_checkout)
        db.commit()
        db.refresh(db_checkout)
        return

    async def process_transaction_ended(
        self, transaction_event: TransactionEventRequest
    ) -> None:
        db: Session = next(get_db())
        db_checkout = self.find_checkout_for_event(db, transaction_event)
        if db_checkout is None:
            info(
                " [CitrineOS] Checkout not found for transaction end event: %r",
                transaction_event,
            )
            return

        db_checkout = self.update_checkout_with_meter_values(
            transaction_event=transaction_event, db_checkout=db_checkout
        )
        db_checkout.transaction_end_time = transaction_event.timestamp
        db.add(db_checkout)
        db.commit()
        db.refresh(db_checkout)

        await self.capture_payment_transaction(app=None, checkout_id=db_checkout.id)

        return

    def update_checkout_with_meter_values(
        self, transaction_event: TransactionEventRequest, db_checkout: CheckoutModel
    ) -> CheckoutModel:
        if (
            transaction_event.meterValue is not None
            and len(transaction_event.meterValue) > 0
        ):
            latest_meter_value = transaction_event.meterValue[
                len(transaction_event.meterValue) - 1
            ]
            for sampled_value in latest_meter_value.sampledValue:
                # unitOfMeasure is optional in OCPP 2.0.1 (e.g. the simulator
                # omits it on the Transaction.End sample). Default to the OCPP
                # default unit (Wh) / no multiplier instead of crashing on None.
                uom = sampled_value.unitOfMeasure
                unit = uom.unit if uom is not None else None
                multiplier = uom.multiplier if uom is not None else None

                if (
                    sampled_value.measurand
                    == MeasurandEnumType.EnergyActiveImportRegister
                    and sampled_value.phase is None
                ):
                    new_kwh_value = sampled_value.value
                    if unit is None or unit == "Wh":
                        new_kwh_value = new_kwh_value / 1000
                    if multiplier is not None:
                        new_kwh_value = new_kwh_value * 10**multiplier
                    if db_checkout.transaction_last_meter_reading is None:
                        db_checkout.transaction_kwh = 0
                    if db_checkout.transaction_last_meter_reading is not None:
                        db_checkout.transaction_kwh += (
                            new_kwh_value - db_checkout.transaction_last_meter_reading
                        )
                    db_checkout.transaction_last_meter_reading = new_kwh_value

                elif (
                    sampled_value.measurand == MeasurandEnumType.PowerActiveImport
                    and sampled_value.phase is None
                ):
                    new_power_value = sampled_value.value
                    if unit is None or unit == "W":
                        new_power_value = new_power_value / 1000
                    if multiplier is not None:
                        new_power_value = new_power_value * 10**multiplier
                    db_checkout.power_active_import = new_power_value

                elif (
                    sampled_value.measurand == MeasurandEnumType.SoC
                    and sampled_value.phase is None
                ):
                    new_soc_value = sampled_value.value
                    if multiplier is not None:
                        new_soc_value = new_soc_value * 10**multiplier
                    db_checkout.transaction_soc = new_soc_value
        return db_checkout

    def _next_display_message_id(self, db: Session, station_id: str) -> int:
        """Next free OCPP SetDisplayMessage id for a station (ids are unique per
        station). Mirrors the scan-and-charge id allocation."""
        most_recent = (
            db.query(MessageInfoModel)
            .filter(MessageInfoModel.stationId == station_id)
            .order_by(MessageInfoModel.id.desc())
            .first()
        )
        return 0 if most_recent is None else most_recent.id + 1

    def _clear_display_message(
        self, station_id: str, tenant_id: str, message_id: int
    ) -> None:
        self.send_citrineos_message(
            station_id=station_id,
            tenant_id=tenant_id,
            url_path="configuration/clearDisplayMessage",
            json_payload={"id": message_id},
        )

    def _evse_tariff_price(self, evse: EvseModel) -> "tuple[float, str]":
        """(price_per_kwh, currency) for an EVSE's connector tariff, or (0, "")
        if none is wired. Used by display adapters that show pricing on-screen
        (e.g. Renova); the standard image adapter ignores it."""
        try:
            connector = evse.connectors[0] if evse.connectors else None
            tariff = connector.tariff if connector is not None else None
            if tariff is not None:
                return (tariff.price_kwh or 0.0), (tariff.currency or "")
        except Exception:
            pass
        return 0.0, ""

    async def push_standing_qr(self, db: Session, evse: EvseModel) -> None:
        """Display the persistent 'scan to pay' QR on an idle charger.

        The QR encodes the PayServe charger-info page (``/checkout/{evse_id}``)
        with no Stripe link baked in: the page reads tariff pricing live and
        creates a checkout per driver (web-portal / pay-before-plug flow), so the
        same QR is reusable across drivers and never goes stale. Pushed when the
        charger comes online and after a catalog sync. Best-effort: if the charger
        is offline the message simply doesn't reach it, and the next online
        StatusNotification re-pushes.

        How the QR is delivered depends on the charger family -- resolved via the
        EVSE's display_adapter_type to a ChargerDisplayAdapter (see
        charger_display.py).
        """
        self._resolve_display_adapter_type(db, evse)
        payment_url = f"{Config.CLIENT_URL}/checkout/{evse.evse_id}"
        price, currency = self._evse_tariff_price(evse)
        adapter = get_display_adapter(evse)
        await adapter.show_payment_qr(
            self, db, evse, payment_url=payment_url, price=price, currency=currency
        )

    async def clear_standing_qr(self, db: Session, evse: EvseModel) -> None:
        """Remove the standing pay QR (charger in use / unavailable)."""
        self._resolve_display_adapter_type(db, evse)
        adapter = get_display_adapter(evse)
        await adapter.clear_payment_qr(self, db, evse)

    def _station_vendor(self, db: Session, station_id: str, tenant_id: str):
        """The charger's reported vendor (ChargingStations.chargePointVendor) for
        a station, or None. Read straight from the shared CitrineOS table."""
        try:
            row = db.execute(
                sql_text(
                    'SELECT "chargePointVendor" FROM "ChargingStations" '
                    'WHERE "ocppConnectionName" = :sid AND "tenantId" = :tid '
                    "LIMIT 1"
                ),
                {"sid": station_id, "tid": int(tenant_id)},
            ).first()
            return row[0] if row else None
        except Exception as e:
            warning(
                " [CitrineOS] charger vendor lookup failed for %s: %r",
                station_id,
                e.__str__(),
            )
            return None

    def _resolve_display_adapter_type(self, db: Session, evse: EvseModel) -> None:
        """Keep display_adapter_type in step with the charger's BootNotification
        vendor. Runs on every standing-QR push -- i.e. on a status change *and*
        after a catalog sync -- so a vendor change (reflash, swapped unit, edited
        BootNotification) is picked up automatically, in either direction. When
        the charger hasn't reported a vendor yet, the current value is left as-is
        (the next push, once it has booted, will set it)."""
        vendor = self._station_vendor(db, evse.station_id, evse.tenant_id)
        if not vendor:
            return
        new_type = display_adapter_for_vendor(vendor) or DEFAULT_ADAPTER
        if evse.display_adapter_type != new_type:
            evse.display_adapter_type = new_type
            db.add(evse)
            db.commit()
            info(
                " [CitrineOS] EVSE %s: set display_adapter_type=%r from vendor %r",
                evse.evse_id,
                new_type,
                vendor,
            )

    async def process_boot_notification(
        self,
        citrine_os_event_headers: CitrineOSeventHeaders,
    ) -> None:
        """A rebooted charger comes back with a blank screen, but our display
        debounce marker (display_message_id) survives in the DB -- so forget
        the marker and re-push the standing QR for every payable connector of
        the station. Best-effort: display problems must not affect boots."""
        db: Session = next(get_db())
        evses = (
            db.query(EvseModel)
            .filter(EvseModel.station_id == citrine_os_event_headers.stationId)
            .all()
        )
        for db_evse in evses:
            try:
                db_evse.display_message_id = None
                db.add(db_evse)
                db.commit()
                if db_evse.status in ("Available", "Occupied"):
                    await self.push_standing_qr(db, db_evse)
                    info(
                        f" [CitrineOS] Boot: standing QR re-pushed to "
                        f"{db_evse.evse_id}"
                    )
            except Exception as e:
                exception(
                    " [CitrineOS] Boot-time standing-QR push failed: %r",
                    e.__str__(),
                )
        return

    async def process_status_notification(
        self,
        status_notification: StatusNotificationRequest,
        citrine_os_event_headers: CitrineOSeventHeaders,
    ) -> None:
        db: Session = next(get_db())
        db_evse = (
            db.query(EvseModel)
            .filter(EvseModel.station_id == citrine_os_event_headers.stationId)
            .filter(EvseModel.ocpp_evse_id == status_notification.evseId)
            .first()
        )
        if db_evse is None:
            info(
                " [CitrineOS] EVSE not found for status notification event: %r",
                status_notification,
            )
            return

        previous_status = db_evse.status
        db_evse.status = status_notification.connectorStatus
        db.add(db_evse)
        db.commit()
        db.refresh(db_evse)

        # Standing "scan to pay" QR: with prepayment enforced the driver plugs
        # in FIRST and then pays, so the QR must stay up while the connector is
        # Occupied-but-unpaid, not just while Available. It is cleared when the
        # paid (or RFID) session actually starts -- see
        # process_transaction_started_remote -- and for Faulted/Unavailable/
        # Reserved states here. Only act on a transition (or a first-time
        # display) so we don't re-push on every repeated StatusNotification.
        # Best-effort -- a display failure must not break status tracking.
        qr_states = ("Available", "Occupied")
        try:
            if db_evse.status in qr_states and (
                previous_status not in qr_states or db_evse.display_message_id is None
            ):
                await self.push_standing_qr(db, db_evse)
            elif (
                db_evse.status not in qr_states
                and db_evse.display_message_id is not None
            ):
                await self.clear_standing_qr(db, db_evse)
        except Exception as e:
            exception(" [CitrineOS] Standing-QR update failed: %r", e.__str__())
        return
