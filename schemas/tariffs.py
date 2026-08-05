from pydantic import BaseModel, ConfigDict


class TariffBase(BaseModel):
    id: int
    price_kwh: float | None
    price_minute: float | None
    price_session: float | None
    currency: str
    tax_rate: float
    # Percentage added on the session total for card processing. Exposed so
    # the checkout page can disclose it BEFORE payment (CTEP: every price
    # component must be shown before the session is activated).
    payment_fee: float | None = None
    authorization_amount: float
    # Add other fields as needed


class TariffCreate(TariffBase):
    pass


class Tariff(TariffBase):
    model_config = ConfigDict(from_attributes=True)
