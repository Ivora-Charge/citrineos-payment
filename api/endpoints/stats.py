"""Charging revenue statistics (dashboards).

Realized revenue lives here in the payment DB (captured_amount +
overage_amount recorded at settlement, backfilled from Stripe for history),
so the dashboards read one cheap aggregate instead of round-tripping to
Stripe. Energy (kWh) statistics come from the CitrineOS Transactions table on
the operator-ui side; this endpoint only owns money.

Service-to-service (X-Catalog-Sync-Secret), called from operator-ui server
actions which enforce that tenant users only see their own tenant.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.endpoints.catalog import require_sync_secret
from config import Config
from db.init_db import get_db

router = APIRouter()

WINDOWS = {
    "today": "date_trunc('day', NOW())",
    "days7": "NOW() - interval '7 days'",
    "days30": "NOW() - interval '30 days'",
    "total": "'-infinity'::timestamptz",
}


class RevenueBucket(BaseModel):
    sessions: int
    revenue_subunits: int


class TenantRevenue(BaseModel):
    tenant_id: Optional[str] = None
    currency: str
    today: RevenueBucket
    days7: RevenueBucket
    days30: RevenueBucket
    total: RevenueBucket


class RevenueSummaryResponse(BaseModel):
    tenants: list[TenantRevenue]


@router.get(
    "/revenue",
    response_model=RevenueSummaryResponse,
    dependencies=[Depends(require_sync_secret)],
)
async def revenue_summary(
    tenant_id: Optional[str] = None, db: Session = Depends(get_db)
):
    """Captured + overage revenue per tenant and currency, bucketed by
    settlement time (captured_at). Omit tenant_id for all tenants."""
    per_window = ", ".join(
        f"COUNT(*) FILTER (WHERE c.captured_at >= {since}) AS {name}_sessions, "
        f"COALESCE(SUM(COALESCE(c.captured_amount, 0) + COALESCE(c.overage_amount, 0)) "
        f"FILTER (WHERE c.captured_at >= {since}), 0) AS {name}_revenue"
        for name, since in WINDOWS.items()
    )
    p = Config.DB_TABLE_PREFIX
    rows = db.execute(
        text(
            f"SELECT e.tenant_id, LOWER(t.currency) AS currency, {per_window} "
            f'FROM "{p}checkouts" c '
            f'JOIN "{p}connectors" pc ON pc.id = c.connector_id '
            f'JOIN "{p}evses" e ON e.id = pc.evse_id '
            f'JOIN "{p}tariffs" t ON t.id = c.tariff_id '
            "WHERE c.captured_at IS NOT NULL "
            "AND (:tid IS NULL OR e.tenant_id = :tid) "
            "GROUP BY e.tenant_id, LOWER(t.currency) "
            "ORDER BY e.tenant_id"
        ),
        {"tid": tenant_id},
    ).mappings()
    return RevenueSummaryResponse(
        tenants=[
            TenantRevenue(
                tenant_id=r["tenant_id"],
                currency=r["currency"] or "usd",
                **{
                    name: RevenueBucket(
                        sessions=r[f"{name}_sessions"],
                        revenue_subunits=int(r[f"{name}_revenue"]),
                    )
                    for name in WINDOWS
                },
            )
            for r in rows
        ]
    )
