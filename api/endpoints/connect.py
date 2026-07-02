"""Stripe Connect onboarding (multi-tenant rollout Phase 5).

Replaces the "paste your acct_... id" flow: the operator-ui asks this service
for a hosted onboarding link, Stripe walks the tenant through KYC, and the
account id is stored on the tenant row the moment the account is created (its
readiness is then queried live from Stripe, so no webhook round-trip is needed
to unblock the UI).

The Stripe platform key lives only in this service, and the Tenants table is
in the shared CitrineOS database, so this is the natural home for the flow.
Both endpoints are service-to-service (X-Catalog-Sync-Secret), called from
operator-ui server actions -- never directly from a browser.
"""

from logging import info
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.endpoints.catalog import require_sync_secret
from db.init_db import get_db

router = APIRouter()


class OnboardingLinkRequest(BaseModel):
    tenant_id: int
    return_url: str
    refresh_url: str


class OnboardingLinkResponse(BaseModel):
    url: str
    stripe_account_id: str


class ConnectStatusResponse(BaseModel):
    stripe_account_id: Optional[str] = None
    charges_enabled: bool = False
    details_submitted: bool = False
    # Why charges are still disabled (e.g. "requirements.past_due") and what
    # Stripe wants next (e.g. ["individual.verification.document"]) -- lets
    # the UI say "action needed: upload ID" instead of a vague "reviewing".
    disabled_reason: Optional[str] = None
    requirements_due: list[str] = []


def _tenant_account_id(db: Session, tenant_id: int) -> Optional[str]:
    row = db.execute(
        text('SELECT "stripeAccountId" FROM "Tenants" WHERE id = :tid'),
        {"tid": tenant_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return row[0]


@router.post(
    "/onboarding-link",
    response_model=OnboardingLinkResponse,
    dependencies=[Depends(require_sync_secret)],
)
async def create_onboarding_link(
    payload: OnboardingLinkRequest, db: Session = Depends(get_db)
):
    """Create (or reuse) the tenant's Standard account and mint a hosted
    onboarding link. Re-calling is safe: an unfinished account just gets a
    fresh link. The sentinel value 'platform' (dev mode: charge on the
    platform account) is replaced by a real account."""
    account_id = _tenant_account_id(db, payload.tenant_id)

    try:
        if not account_id or not account_id.startswith("acct_"):
            account = stripe.Account.create(type="standard")
            account_id = account.id
            db.execute(
                text('UPDATE "Tenants" SET "stripeAccountId" = :acct WHERE id = :tid'),
                {"acct": account_id, "tid": payload.tenant_id},
            )
            db.commit()
            info(
                " [connect] Created Stripe account %s for tenant %s",
                account_id,
                payload.tenant_id,
            )

        link = stripe.AccountLink.create(
            account=account_id,
            type="account_onboarding",
            return_url=payload.return_url,
            refresh_url=payload.refresh_url,
        )
    except stripe.error.StripeError as e:
        # e.g. "You can only create new accounts if you've signed up for
        # Connect" -- a one-time platform-account setup step in the Stripe
        # dashboard. Surface the reason instead of a bare 500 so the UI toast
        # is actionable.
        raise HTTPException(status_code=502, detail=f"Stripe: {e.user_message or str(e)}")
    return OnboardingLinkResponse(url=link.url, stripe_account_id=account_id)


@router.get(
    "/status",
    response_model=ConnectStatusResponse,
    dependencies=[Depends(require_sync_secret)],
)
async def connect_status(tenant_id: int, db: Session = Depends(get_db)):
    """Live readiness of the tenant's Stripe account (no webhook needed)."""
    account_id = _tenant_account_id(db, tenant_id)
    if not account_id or not account_id.startswith("acct_"):
        return ConnectStatusResponse(stripe_account_id=account_id)
    account = stripe.Account.retrieve(account_id)
    requirements = account.get("requirements") or {}
    return ConnectStatusResponse(
        stripe_account_id=account_id,
        charges_enabled=bool(account.get("charges_enabled")),
        details_submitted=bool(account.get("details_submitted")),
        disabled_reason=requirements.get("disabled_reason"),
        requirements_due=list(
            dict.fromkeys(
                (requirements.get("past_due") or [])
                + (requirements.get("currently_due") or [])
            )
        ),
    )
