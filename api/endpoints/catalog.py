"""Service-to-service catalog sync API.

Lets the operator-ui onboarding flow (or any trusted backend) create/update the
payment_* catalog chain so operators no longer have to run seed.py by hand.

All write/read endpoints require the shared secret in the X-Catalog-Sync-Secret
header (see config.PAYMENT_CATALOG_SYNC_SECRET). If the secret is not configured
the endpoints fail closed (503) -- the catalog is never writable anonymously.
"""

from logging import error, info
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from config import Config
from db.init_db import Evse as EvseModel, Location as LocationModel, get_db
from catalog.sync import upsert_payment_catalog
from schemas.catalog import (
    CatalogStatusResponse,
    CatalogSyncRequest,
    CatalogSyncResponse,
)

router = APIRouter()


def require_sync_secret(
    x_catalog_sync_secret: Optional[str] = Header(default=None),
) -> None:
    """Fail closed when no secret is configured; reject mismatches with 401."""
    expected = Config.PAYMENT_CATALOG_SYNC_SECRET
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Catalog sync API disabled: PAYMENT_CATALOG_SYNC_SECRET not set",
        )
    if x_catalog_sync_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid catalog sync secret")


@router.post("/sync", response_model=CatalogSyncResponse)
async def sync_catalog(
    payload: CatalogSyncRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_sync_secret),
):
    try:
        result = upsert_payment_catalog(
            db,
            operator_name=payload.operator_name,
            stripe_account_id=payload.stripe_account_id,
            location_id=payload.location_id,
            address=payload.address,
            postal_code=payload.postal_code,
            city=payload.city,
            state=payload.state,
            country=payload.country,
            station_id=payload.station_id,
            tenant_id=payload.tenant_id,
            ocpp_evse_id=payload.ocpp_evse_id,
            evse_id=payload.evse_id,
            connector_id=payload.connector_id,
            currency=payload.currency,
            tax_rate=payload.tax_rate,
            authorization_amount=payload.authorization_amount,
            price_kwh=payload.price_kwh,
            price_minute=payload.price_minute,
            price_session=payload.price_session,
            payment_fee=payload.payment_fee,
            power_type=payload.power_type.value,
            max_voltage=payload.max_voltage,
            max_amperage=payload.max_amperage,
        )

        info(f" [catalog] SYNC SUCCESS for evse_id={payload.evse_id}")
        info(f" [catalog] payload={payload}")
        db.commit()
    except Exception as exc:  # noqa: BLE001 - surface as 500, keep the DB clean
        db.rollback()
        error(f" [catalog] SYNC ERROR for evse_id={payload.evse_id}: {exc}")
        raise HTTPException(status_code=500, detail="Catalog sync failed")

    # Show (or refresh) the standing "scan to pay" QR on the just-synced charger.
    # Best-effort: a freshly-onboarded or offline charger may not be reachable, in
    # which case the next online StatusNotification re-pushes it.
    try:
        ocpp_integration = request.app.ocpp_integration
        evse = (
            db.query(EvseModel).filter(EvseModel.evse_id == payload.evse_id).first()
        )
        if evse is not None:
            await ocpp_integration.push_standing_qr(db, evse)
    except Exception as exc:  # noqa: BLE001 - QR display must not fail the sync
        error(f" [catalog] QR push after sync failed for {payload.evse_id}: {exc}")

    return result


@router.get("/status", response_model=CatalogStatusResponse)
async def catalog_status(
    evse_id: Optional[str] = Query(default=None),
    station_id: Optional[str] = Query(default=None),
    tenant_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_sync_secret),
):
    if evse_id:
        evse = db.query(EvseModel).filter(EvseModel.evse_id == evse_id).first()
    elif station_id and tenant_id:
        evse = (
            db.query(EvseModel)
            .filter(
                EvseModel.station_id == station_id,
                EvseModel.tenant_id == tenant_id,
            )
            .first()
        )
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide evse_id, or both station_id and tenant_id",
        )

    if evse is None:
        return CatalogStatusResponse(exists=False)

    location = (
        db.query(LocationModel).filter(LocationModel.id == evse.location_id).first()
    )
    return CatalogStatusResponse(
        exists=True,
        evse_id=evse.id,
        location_id=evse.location_id,
        operator_id=location.operator_id if location else None,
    )


@router.post("/sync-station")
async def sync_station(_: None = Depends(require_sync_secret)):
    """Pull a station's EVSEs/connectors from the CitrineOS data API
    (Config.CITRINEOS_DATA_API_URL) and upsert them. Not implemented yet -- this
    is the Phase 3 automation hook; use POST /sync with explicit data for now."""
    raise HTTPException(
        status_code=501,
        detail="sync-station not implemented; use POST /catalog/sync",
    )
