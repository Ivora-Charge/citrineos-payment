"""Connect commission: integer basis points, charged on actual gross receipts.

Rates are frozen at checkout creation. Legacy checkouts have NULL (no fee).
The fee is deducted from the tenant proceeds, never added to the driver bill.
"""
from db.init_db import TenantPlatformFee

DEFAULT_PLATFORM_FEE_BPS = 1000


def tenant_rate(db, tenant_id):
    row = db.query(TenantPlatformFee).filter_by(tenant_id=int(tenant_id)).first()
    return DEFAULT_PLATFORM_FEE_BPS if row is None else row.basis_points


def checkout_rate(db, evse, account_id):
    if not account_id or not account_id.startswith("acct_"):
        return 0
    return tenant_rate(db, evse.tenant_id)


def fee_amount(amount, basis_points):
    bps = 0 if basis_points is None else basis_points
    if type(bps) is not int or not 0 <= bps <= 10000:
        raise ValueError("Invalid platform fee basis points")
    if int(amount) != amount or amount < 0:
        raise ValueError("Fee base must be nonnegative integer currency subunits")
    return (int(amount) * bps + 5000) // 10000


def fee_kwargs(amount, basis_points, account_id):
    if not account_id or not account_id.startswith("acct_") or basis_points is None:
        return {}
    return {"application_fee_amount": fee_amount(amount, basis_points)}
