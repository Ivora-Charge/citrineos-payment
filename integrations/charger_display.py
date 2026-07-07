"""Charger display adapters: how to put the payment QR on a charger's screen.

Different charger families accept the QR differently. The standard, spec-compliant
way is OCPP 2.0.1 ``SetDisplayMessage`` with a server-rendered QR *image*; some
vendors (e.g. Renova / Rainbow "rcd") instead take a *URL* over a vendor
``DataTransfer`` and render the QR on the device themselves.

To support a new charger family, add a ``ChargerDisplayAdapter`` subclass and
register it in ``ADAPTERS`` keyed by the ``display_adapter_type`` stored on the
EVSE.
Nothing else in the payment flow needs to change -- ``push_standing_qr`` /
``clear_standing_qr`` resolve the right adapter via ``get_display_adapter``.

``ocpp`` passed to the adapters is the ``CitrineOSIntegration`` instance, which
provides ``send_citrineos_message``, ``fileIntegration`` (file upload) and the
display-message id helpers.
"""

import json
from io import BytesIO
from logging import info

import qrcode

# Stored in ``Evse.display_message_id`` for adapters that don't use an OCPP
# display-message id (e.g. DataTransfer) purely to mark "a QR is currently shown"
# so the status-notification debounce works. Any non-null value means "shown".
SHOWN_SENTINEL = -1


class ChargerDisplayAdapter:
    """Strategy for showing/clearing the payment QR on one charger family."""

    async def show_payment_qr(
        self, ocpp, db, evse, *, payment_url: str, price, currency: str
    ) -> None:
        raise NotImplementedError

    async def clear_payment_qr(self, ocpp, db, evse) -> None:
        raise NotImplementedError

    async def show_transaction_qr(
        self,
        ocpp,
        db,
        evse,
        *,
        payment_url: str,
        transaction_id: str,
        price,
        currency: str,
    ) -> "int | None":
        """Show the scan-and-charge (transaction-bound) QR while an unauthorized
        session waits for payment. Returns a display-message id to store on the
        checkout for later clearing, or None if the adapter uses no id (e.g.
        DataTransfer)."""
        raise NotImplementedError

    async def clear_transaction_qr(self, ocpp, evse, *, message_id) -> None:
        raise NotImplementedError


class StandardDisplayAdapter(ChargerDisplayAdapter):
    """OCPP 2.0.1 ``SetDisplayMessage`` with a server-rendered QR image (URI
    format). The charger just displays the image, so this works on any
    spec-compliant 2.0.1 charger. ``price``/``currency`` are unused (the image
    encodes only the URL)."""

    async def show_payment_qr(self, ocpp, db, evse, *, payment_url, price, currency):
        # The encoded URL is static per EVSE, so the image never changes: render
        # and upload once, reuse the asset URL afterwards.
        if not evse.qr_image_url:
            qr_img = qrcode.make(payment_url)
            buffer = BytesIO()
            qr_img.save(buffer)
            buffer.seek(0)
            evse.qr_image_url = ocpp.fileIntegration.upload_file(
                buffer,
                "image/png",
                f"qrcode_{evse.evse_id}.png",
                f"QRCode_{evse.evse_id}",
            )

        if evse.display_message_id is not None:
            ocpp._clear_display_message(
                evse.station_id, evse.tenant_id, evse.display_message_id
            )

        next_id = ocpp._next_display_message_id(db, evse.station_id)
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/setDisplayMessage",
            json_payload={
                "message": {
                    "id": next_id,
                    "priority": "AlwaysFront",
                    "state": "Idle",
                    "message": {"format": "URI", "content": evse.qr_image_url},
                }
            },
        )
        evse.display_message_id = next_id
        db.add(evse)
        db.commit()

    async def clear_payment_qr(self, ocpp, db, evse):
        if evse.display_message_id is None:
            return
        ocpp._clear_display_message(
            evse.station_id, evse.tenant_id, evse.display_message_id
        )
        evse.display_message_id = None
        db.add(evse)
        db.commit()

    async def show_transaction_qr(
        self, ocpp, db, evse, *, payment_url, transaction_id, price, currency
    ):
        # Transaction-bound QR: rendered fresh (the url carries the per-session
        # ?pay= link) and tagged with the transactionId so it stays up for this
        # session. Not cached like the standing QR.
        qr_img = qrcode.make(payment_url)
        buffer = BytesIO()
        qr_img.save(buffer)
        buffer.seek(0)
        image_url = ocpp.fileIntegration.upload_file(
            buffer,
            "image/png",
            f"qrcode_{evse.station_id}_{transaction_id}.png",
            f"QRCode_{evse.station_id}_{transaction_id}",
        )
        next_id = ocpp._next_display_message_id(db, evse.station_id)
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/setDisplayMessage",
            json_payload={
                "message": {
                    "id": next_id,
                    "priority": "AlwaysFront",
                    "transactionId": transaction_id,
                    "message": {"format": "URI", "content": image_url},
                }
            },
        )
        return next_id

    async def clear_transaction_qr(self, ocpp, evse, *, message_id):
        if message_id is None:
            return
        ocpp._clear_display_message(evse.station_id, evse.tenant_id, message_id)

class Renova21DisplayAdapter(ChargerDisplayAdapter):
    """Renova chargers on OCPP 2.x take the OCPP *2.1* display-message QR
    format: ``SetDisplayMessage`` with ``format: "QRCODE"`` and the payment
    *URL* as content -- the charger renders the QR itself, so no image is
    rendered or uploaded server-side. (QRCODE is not in the 2.0.1 enum; our
    CitrineOS schemas are extended to pass it through to 2.0.1 connections.)
    ``price``/``currency`` are unused (the QR encodes only the URL)."""

    async def show_payment_qr(self, ocpp, db, evse, *, payment_url, price, currency):
        if evse.display_message_id is not None:
            ocpp._clear_display_message(
                evse.station_id, evse.tenant_id, evse.display_message_id
            )

        next_id = ocpp._next_display_message_id(db, evse.station_id)
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/setDisplayMessage",
            json_payload={
                "message": {
                    "id": next_id,
                    "priority": "AlwaysFront",
                    "state": "Idle",
                    "message": {"format": "QRCODE", "content": payment_url},
                }
            },
        )
        evse.display_message_id = next_id
        db.add(evse)
        db.commit()

    async def clear_payment_qr(self, ocpp, db, evse):
        if evse.display_message_id is None:
            return
        ocpp._clear_display_message(
            evse.station_id, evse.tenant_id, evse.display_message_id
        )
        evse.display_message_id = None
        db.add(evse)
        db.commit()

    async def show_transaction_qr(
        self, ocpp, db, evse, *, payment_url, transaction_id, price, currency
    ):
        # Transaction-bound QR: the url carries the per-session ?pay= link and
        # the message is tagged with the transactionId so it stays up for this
        # session. Same QRCODE format as the standing QR.
        next_id = ocpp._next_display_message_id(db, evse.station_id)
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/setDisplayMessage",
            json_payload={
                "message": {
                    "id": next_id,
                    "priority": "AlwaysFront",
                    "transactionId": transaction_id,
                    "message": {"format": "QRCODE", "content": payment_url},
                }
            },
        )
        return next_id

    async def clear_transaction_qr(self, ocpp, evse, *, message_id):
        if message_id is None:
            return
        ocpp._clear_display_message(evse.station_id, evse.tenant_id, message_id)

class RenovaDisplayAdapter(ChargerDisplayAdapter):
    """Renova / Rainbow ("rcd") chargers render the QR from a URL themselves. The
    URL (plus pricing) is delivered with a vendor ``DataTransfer``:

        DataTransfer { vendorId: "rcd", messageId: "qrcode_req",
                       data: "{connector_id, evse_id, price, unit, url}" }

    No image is rendered or uploaded server-side. ``data`` is a JSON *string*.
    """

    VENDOR_ID = "rcd"
    SHOW_MESSAGE_ID = "qrcode_req"

    def _data_transfer(self, ocpp, evse, payload: dict) -> None:
        ocpp.send_citrineos_message(
            station_id=evse.station_id,
            tenant_id=evse.tenant_id,
            url_path="configuration/dataTransfer",
            json_payload={
                "vendorId": self.VENDOR_ID,
                "messageId": self.SHOW_MESSAGE_ID,
                "data": json.dumps(payload),
            },
        )

    async def show_payment_qr(self, ocpp, db, evse, *, payment_url, price, currency):
        # NOTE: connector_id / evse_id mapping mirrors the vendor's documented
        # example; confirm with Renova whether connector_id should be the OCPP
        # evseId or the physical connector number for multi-connector units.
        self._data_transfer(
            ocpp,
            evse,
            {
                "connector_id": evse.ocpp_evse_id or 1,
                "evse_id": evse.evse_id or "",
                "price": float(price) if price is not None else 0.0,
                "unit": f"{(currency or '').upper()}/kWh",
                "url": payment_url,
            },
        )
        evse.display_message_id = SHOWN_SENTINEL  # DataTransfer has no display id
        db.add(evse)
        db.commit()

    async def clear_payment_qr(self, ocpp, db, evse):
        if evse.display_message_id is None:
            return
        # Best-effort: re-send with an empty url to take the QR down. Renova's
        # explicit clear message isn't documented yet -- confirm with the vendor.
        self._data_transfer(
            ocpp,
            evse,
            {
                "connector_id": evse.ocpp_evse_id or 1,
                "evse_id": evse.evse_id or "",
                "price": 0.0,
                "unit": "",
                "url": "",
            },
        )
        evse.display_message_id = None
        db.add(evse)
        db.commit()

    async def show_transaction_qr(
        self, ocpp, db, evse, *, payment_url, transaction_id, price, currency
    ):
        # Same DataTransfer as the standing QR, but the url carries the per-session
        # ?pay= link. The device renders it; there's no display-message id.
        self._data_transfer(
            ocpp,
            evse,
            {
                "connector_id": evse.ocpp_evse_id or 1,
                "evse_id": evse.evse_id or "",
                "price": float(price) if price is not None else 0.0,
                "unit": f"{(currency or '').upper()}/kWh",
                "url": payment_url,
            },
        )
        return None

    async def clear_transaction_qr(self, ocpp, evse, *, message_id):
        self._data_transfer(
            ocpp,
            evse,
            {
                "connector_id": evse.ocpp_evse_id or 1,
                "evse_id": evse.evse_id or "",
                "price": 0.0,
                "unit": "",
                "url": "",
            },
        )


class NoDisplayAdapter(ChargerDisplayAdapter):
    """Chargers with no reachable display channel — e.g. generic OCPP 1.6
    units (1.6 has no SetDisplayMessage and no known vendor DataTransfer for
    them). The standing QR is expected to be a printed static code: the
    encoded /checkout/{evse_id} URL is stable per EVSE, so a sticker works.
    All display operations are no-ops that keep the debounce marker sane."""

    async def show_payment_qr(self, ocpp, db, evse, *, payment_url, price, currency):
        if evse.display_message_id is None:
            info(
                " [charger_display] EVSE %s has no display channel; standing QR"
                " must be a printed static code for %s",
                getattr(evse, "evse_id", "?"),
                payment_url,
            )
        evse.display_message_id = SHOWN_SENTINEL
        db.add(evse)
        db.commit()

    async def clear_payment_qr(self, ocpp, db, evse):
        evse.display_message_id = None
        db.add(evse)
        db.commit()

    async def show_transaction_qr(
        self, ocpp, db, evse, *, payment_url, transaction_id, price, currency
    ):
        return None

    async def clear_transaction_qr(self, ocpp, evse, *, message_id):
        return None


# Registry: display_adapter_type -> adapter instance. Add new charger families here.
ADAPTERS = {
    "standard": StandardDisplayAdapter(),
    "renova": RenovaDisplayAdapter(),
    "renova21": Renova21DisplayAdapter(),
    "none": NoDisplayAdapter(),
}
DEFAULT_ADAPTER = "standard"

# Maps the CitrineOS BootNotification vendor (ChargingStations.chargePointVendor)
# to a display_adapter_type, matched case-insensitively. The payment service
# refreshes each EVSE's display_adapter_type from this on every standing-QR push
# (status change + catalog sync), so it tracks the charger's reported vendor
# and connection protocol. Extend these as more vendors are onboarded.
# Renova ("RCD") delivery depends on the OCPP protocol the unit connected
# with: 2.x units take SetDisplayMessage in the OCPP 2.1 QRCODE format
# ("renova21"); older/1.6 units take the vendor DataTransfer ("renova").
# Keys must be UPPERCASE: lookups normalize the reported vendor with .upper().
DISPLAY_ADAPTER_BY_VENDOR = {
    "RCD": "renova",
    "RENOVA": "renova",
}
DISPLAY_ADAPTER_BY_VENDOR_OCPP2 = {
    "RCD": "renova21",
    "RENOVA": "renova21",
}


def display_adapter_for_vendor(vendor, protocol=None) -> "str | None":
    """Map a charger's reported vendor (and OCPP connection protocol, e.g.
    ``ocpp2.0.1``) to a display_adapter_type, or None."""
    if not vendor:
        return None
    key = str(vendor).strip().upper()
    if protocol and str(protocol).strip().lower().startswith("ocpp2"):
        adapter = DISPLAY_ADAPTER_BY_VENDOR_OCPP2.get(key)
        if adapter:
            return adapter
    return DISPLAY_ADAPTER_BY_VENDOR.get(key)


def get_display_adapter(evse) -> ChargerDisplayAdapter:
    """Resolve the display adapter for an EVSE from its ``display_adapter_type``
    (falls back to the standard SetDisplayMessage adapter for unknown/unset
    types)."""
    adapter_type = (
        getattr(evse, "display_adapter_type", None) or DEFAULT_ADAPTER
    ).lower()
    adapter = ADAPTERS.get(adapter_type)
    if adapter is None:
        info(
            " [charger_display] Unknown display_adapter_type %r for EVSE %s; using %r",
            adapter_type,
            getattr(evse, "evse_id", "?"),
            DEFAULT_ADAPTER,
        )
        adapter = ADAPTERS[DEFAULT_ADAPTER]
    return adapter
