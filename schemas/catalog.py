from typing import Optional

from pydantic import BaseModel, field_validator

from schemas.connectors import ConnectorPowerType
from catalog.sync import (
    DEFAULT_AUTHORIZATION_AMOUNT,
    DEFAULT_CURRENCY,
    DEFAULT_MAX_AMPERAGE,
    DEFAULT_MAX_VOLTAGE,
    DEFAULT_PAYMENT_FEE,
    DEFAULT_POWER_TYPE,
    DEFAULT_PRICE_KWH,
    DEFAULT_PRICE_MINUTE,
    DEFAULT_PRICE_SESSION,
    DEFAULT_TAX_RATE,
)


class CatalogSyncRequest(BaseModel):
    """Upsert one operator -> location -> tariff -> evse -> connector chain.

    Identity / business fields are required. Tariff and connector specs are
    optional and fall back to the historical seed.py defaults so partial
    payloads still produce a working catalog row.
    """

    # Operator
    operator_name: str
    stripe_account_id: str  # "acct_..." for Connect, else charges on platform

    # Location
    location_id: str
    address: str
    postal_code: str
    city: str
    state: str
    country: str

    # EVSE / station
    station_id: str  # CitrineOS ocppConnectionName
    tenant_id: str
    ocpp_evse_id: int
    evse_id: str  # user-facing business key, convention: {station_id}-{ocpp_evse_id}
    connector_id: Optional[str] = None  # defaults to "{evse_id}-1"

    # Tariff (optional, defaulted)
    currency: str = DEFAULT_CURRENCY
    tax_rate: float = DEFAULT_TAX_RATE
    authorization_amount: float = DEFAULT_AUTHORIZATION_AMOUNT
    price_kwh: float = DEFAULT_PRICE_KWH
    price_minute: float = DEFAULT_PRICE_MINUTE
    price_session: float = DEFAULT_PRICE_SESSION
    payment_fee: float = DEFAULT_PAYMENT_FEE

    # Connector specs (optional, defaulted)
    power_type: ConnectorPowerType = ConnectorPowerType(DEFAULT_POWER_TYPE)
    max_voltage: int = DEFAULT_MAX_VOLTAGE
    max_amperage: int = DEFAULT_MAX_AMPERAGE

    @field_validator("power_type", mode="before")
    @classmethod
    def normalize_power_type(cls, v):
        if v == "AC":
            return ConnectorPowerType.AC_1_PHASE
        return v


class CatalogSyncCreated(BaseModel):
    operator: bool
    location: bool
    tariff: bool
    evse: bool
    connector: bool


class CatalogSyncResponse(BaseModel):
    operator_id: int
    location_id: int
    tariff_id: int
    evse_id: int
    connector_id: int
    created: CatalogSyncCreated


class CatalogStatusResponse(BaseModel):
    exists: bool
    evse_id: Optional[int] = None
    location_id: Optional[int] = None
    operator_id: Optional[int] = None
