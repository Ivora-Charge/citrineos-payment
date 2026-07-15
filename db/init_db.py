from logging import info
from config import Config

from sqlalchemy import (
    Boolean,
    UniqueConstraint,
    create_engine,
    Column,
    DateTime,
    ForeignKey,
    Float,
    Integer,
    String,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship


engine = create_engine(
    f"postgresql://{Config.DB_USER}:{Config.DB_PASSWORD}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_DATABASE}",
    # the event consumer opens sessions outside FastAPI's Depends() lifecycle;
    # give the pool headroom and recover stale connections
    pool_size=20,
    max_overflow=30,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Connector(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}connectors"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    connector_id = Column(
        String(36),
        index=True,
        nullable=False,
    )
    power_type = Column(String(20), nullable=False)
    max_voltage = Column(Integer, nullable=False)
    max_amperage = Column(Integer, nullable=False)

    evse_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}evses.id"))
    evse = relationship("Evse", back_populates="connectors")

    tariff_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}tariffs.id"))
    tariff = relationship("Tariff", back_populates="connectors")


class Evse(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}evses"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    evse_id = Column(String(48), index=True, nullable=False, unique=True)
    ocpp_evse_id = Column(Integer, nullable=False)
    status = Column(String(48), nullable=False)
    station_id = Column(String(255), nullable=False)
    tenant_id = Column(String(3), nullable=False)

    # Standing "scan to pay" QR shown on the idle charger display. display_message_id
    # is the OCPP SetDisplayMessage id currently on screen (so it can be cleared /
    # replaced); qr_image_url caches the uploaded QR asset (the encoded URL is
    # static per EVSE, so the image is identical every push).
    display_message_id = Column(Integer)
    qr_image_url = Column(String(512))
    # Selects the charger-display adapter (integrations/charger_display.py): how
    # this charger family wants the payment QR delivered. NULL/"standard" =>
    # OCPP SetDisplayMessage with a rendered image; "renova" => vendor
    # DataTransfer with a URL the device renders itself.
    display_adapter_type = Column(String(32))
    # Digits the driver keys in on the pay-by-phone IVR to pick this charger.
    # Network-unique (callers have no tenant context); assigned by
    # catalog/phone_codes.py and printed on the signage.
    phone_code = Column(String(8), unique=True, index=True)

    connectors = relationship("Connector", back_populates="evse")

    location_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}locations.id"))
    location = relationship("Location", back_populates="evses")


class Location(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}locations"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    location_id = Column(String(36), index=True, nullable=False, unique=True)

    address = Column(
        String(255),
    )
    postal_code = Column(String(10))
    city = Column(String(45))
    state = Column(String(45))
    country = Column(String(3))

    evses = relationship("Evse", back_populates="location")

    operator_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}operators.id"))
    operator = relationship("Operator", back_populates="locations")


class Operator(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}operators"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    name = Column(String(255), index=True, nullable=False, unique=True)
    stripe_account_id = Column(String(255), nullable=False, unique=True)
    locations = relationship("Location", back_populates="operator")


class Tariff(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}tariffs"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    price_kwh = Column(
        Float,
    )
    price_minute = Column(
        Float,
    )
    price_session = Column(
        Float,
    )
    currency = Column(String(3), nullable=False)
    tax_rate = Column(Float, nullable=False)
    authorization_amount = Column(Float, nullable=False)
    payment_fee = Column(Float, nullable=False)
    stripe_price_id = Column(String(255), unique=True)

    connectors = relationship("Connector", back_populates="tariff")


class Checkout(Base):
    __tablename__ = f"{Config.DB_TABLE_PREFIX}checkouts"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    payment_intent_id = Column(String(255), index=True, unique=True)
    # Second off-session PaymentIntent that bills cost above the captured hold
    # (the overage charge). NULL until/unless an overage is charged; set so a
    # duplicate Ended event can't double-charge.
    overage_payment_intent_id = Column(String(255))
    # When the hold was captured. Doubles as the idempotency guard (a second
    # Ended event / reaper pass skips already-settled checkouts).
    captured_at = Column(DateTime(timezone=True))
    # What was actually collected, in currency subunits (cents): the captured
    # part of the hold, and the separate off-session overage charge. Together
    # they are this checkout's realized revenue -- the dashboards sum these
    # instead of round-tripping to Stripe.
    captured_amount = Column(Integer)
    overage_amount = Column(Integer)
    authorization_amount = Column(
        Float,
    )
    connector_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}connectors.id"))
    tariff_id = Column(Integer, ForeignKey(f"{Config.DB_TABLE_PREFIX}tariffs.id"))
    # Payment channel: 'ivr' for pay-by-phone; NULL for the QR/web flows that
    # predate the column. Lets reporting split revenue by channel.
    source = Column(String(8))
    qr_code_message_id = Column(
        Integer,
    )

    remote_request_status = Column(
        String(8),
    )
    remote_request_transaction_id = Column(
        String(36),
    )

    transaction_start_time = Column(
        DateTime(timezone=True),
    )
    transaction_end_time = Column(
        DateTime(timezone=True),
    )
    transaction_last_meter_reading = Column(
        Float,
    )
    transaction_kwh = Column(
        Float,
    )
    power_active_import = Column(
        Float,
    )
    transaction_soc = Column(
        Float,
    )


# CitrineOS Models
# These are not complete.
# See https://github.com/citrineos/citrineos-core/blob/main/01_Data/src/layers/sequelize/model/
# To review full models


# NOTE: column mappings below follow citrineos-core migration 20260427000000
# (rename-charging-station-columns): the station identifier columns are now
# "ocppConnectionName" and the old surrogate ids were renamed. Attribute names
# are kept so the rest of this codebase keeps working.
class OcppEvse(Base):
    __tablename__ = "Evses"

    databaseId = Column("id", Integer, primary_key=True, autoincrement="auto", index=True)
    id = Column("evseTypeId", Integer, nullable=False)

    __table_args__ = ()


class Transaction(Base):
    __tablename__ = "Transactions"

    id = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    stationId = Column("ocppConnectionName", String(255), nullable=False)
    transactionId = Column(String(255), nullable=False)
    isActive = Column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "ocppConnectionName", "transactionId", name="stationId_transactionId"
        ),
    )

    evseDatabaseId = Column("evseId", Integer, ForeignKey("Evses.id"))
    evse = relationship("OcppEvse")


class MessageInfo(Base):
    __tablename__ = "MessageInfos"

    databaseId = Column(Integer, primary_key=True, autoincrement="auto", index=True)
    stationId = Column(
        "ocppConnectionName",
        String(255),
    )
    id = Column(
        Integer,
    )

    __table_args__ = (
        UniqueConstraint("ocppConnectionName", "id", name="stationId_id"),
    )


def init_db() -> None:
    # TODO: add Alembic migrations later
    # info(" [init_db] Deleting database tables.")
    # Base.metadata.drop_all(bind=engine,)
    info(" [init_db] Creating database tables if not exist.")
    Base.metadata.create_all(
        bind=engine,
    )

    # Lightweight forward-migration until Alembic lands: create_all() does not add
    # columns to tables that already exist, so add the standing-QR columns to
    # payment_evses idempotently.
    evses_table = f"{Config.DB_TABLE_PREFIX}evses"
    with engine.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{evses_table}" '
                "ADD COLUMN IF NOT EXISTS display_message_id INTEGER"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{evses_table}" '
                "ADD COLUMN IF NOT EXISTS qr_image_url VARCHAR(512)"
            )
        )
        # Rename the legacy column to display_adapter_type if it's still around
        # (preserves data), then ensure the column exists for fresh installs.
        conn.execute(
            text(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM information_schema.columns "
                f"WHERE table_name = '{evses_table}' "
                "AND column_name = 'charger_type') "
                "AND NOT EXISTS (SELECT 1 FROM information_schema.columns "
                f"WHERE table_name = '{evses_table}' "
                "AND column_name = 'display_adapter_type') THEN "
                f'ALTER TABLE "{evses_table}" '
                "RENAME COLUMN charger_type TO display_adapter_type; "
                "END IF; END $$;"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{evses_table}" '
                "ADD COLUMN IF NOT EXISTS display_adapter_type VARCHAR(32)"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{evses_table}" '
                "ADD COLUMN IF NOT EXISTS phone_code VARCHAR(8)"
            )
        )
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{evses_table}_phone_code "
                f'ON "{evses_table}" (phone_code)'
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{Config.DB_TABLE_PREFIX}checkouts" '
                "ADD COLUMN IF NOT EXISTS overage_payment_intent_id VARCHAR(255)"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{Config.DB_TABLE_PREFIX}checkouts" '
                "ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{Config.DB_TABLE_PREFIX}checkouts" '
                "ADD COLUMN IF NOT EXISTS source VARCHAR(8)"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{Config.DB_TABLE_PREFIX}checkouts" '
                "ADD COLUMN IF NOT EXISTS captured_amount INTEGER"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{Config.DB_TABLE_PREFIX}checkouts" '
                "ADD COLUMN IF NOT EXISTS overage_amount INTEGER"
            )
        )
        # Money received per OCPP transaction, joined for the operator UI:
        # Hasura tracks this view and exposes it as the Transactions.Revenue
        # relationship (see citrineos-core hasura-metadata). Amounts are in
        # currency subunits (cents); overage counts only when its second
        # PaymentIntent was actually created.
        prefix = Config.DB_TABLE_PREFIX
        conn.execute(
            text(
                f"CREATE OR REPLACE VIEW {prefix}transaction_revenue AS "
                "SELECT t.id AS transaction_pk, "
                't."tenantId", '
                "c.id AS checkout_id, "
                "c.captured_amount, "
                "CASE WHEN c.overage_payment_intent_id IS NOT NULL "
                "THEN c.overage_amount END AS overage_amount, "
                "(COALESCE(c.captured_amount, 0) "
                " + COALESCE(CASE WHEN c.overage_payment_intent_id IS NOT NULL "
                "            THEN c.overage_amount END, 0)) AS total_received, "
                "upper(tar.currency) AS currency, "
                "c.captured_at "
                f"FROM {prefix}checkouts c "
                f"JOIN {prefix}connectors pc ON pc.id = c.connector_id "
                f"JOIN {prefix}evses pe ON pe.id = pc.evse_id "
                f"LEFT JOIN {prefix}tariffs tar ON tar.id = c.tariff_id "
                'JOIN "ChargingStations" s '
                "ON s.\"ocppConnectionName\" = pe.station_id "
                "AND s.\"tenantId\" = pe.tenant_id::int "
                'JOIN "Transactions" t '
                "ON t.\"stationId\" = s.id "
                "AND t.\"transactionId\" = c.remote_request_transaction_id "
                "WHERE c.remote_request_transaction_id IS NOT NULL"
            )
        )

    # Every EVSE gets a pay-by-phone code (local import: phone_codes imports
    # the Evse model from this module).
    from catalog.phone_codes import backfill_phone_codes

    db = SessionLocal()
    try:
        backfill_phone_codes(db)
        db.commit()
    finally:
        db.close()


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
