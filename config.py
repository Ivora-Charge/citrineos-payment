import os
from typing import get_type_hints, Union
from dotenv import load_dotenv


class AppConfigError(Exception):
    pass


def _parse_bool(val: Union[str, bool]) -> bool:  # pylint: disable=E1136
    return val if isinstance(val, bool) else val.lower() in ["true", "yes", "1"]


# AppConfig class with required fields, default values, type checking, and typecasting for int and bool values
class AppConfig:
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "[%(asctime)s] %(levelname)s:%(name)s:%(message)s"
    OPENAPI_TITLE: str = "Stackbox Payment API"
    MESSAGE_BROKER_SSL_ACTIVE: bool
    MESSAGE_BROKER_HOST: str
    MESSAGE_BROKER_PORT: int
    MESSAGE_BROKER_USER: str
    MESSAGE_BROKER_PASSWORD: str
    MESSAGE_BROKER_VHOST: str
    MESSAGE_BROKER_EXCHANGE_TYPE: str = "topic"
    MESSAGE_BROKER_EXCHANGE_NAME: str
    MESSAGE_BROKER_EVENT_CONSUMER_QUEUE_NAME: str
    WEBSERVER_HOST: str
    WEBSERVER_PORT: int
    WEBSERVER_PATH: str
    DB_HOST: str
    DB_PORT: int
    DB_DATABASE: str
    DB_USER: str
    DB_PASSWORD: str
    DB_TABLE_PREFIX: str
    STRIPE_API_KEY: str
    STRIPE_ENDPOINT_SECRET_ACCOUNT: str
    STRIPE_ENDPOINT_SECRET_CONNECT: str
    AMPAY_DEFAULT_FEE: float
    AMPAY_COUNTRY_CODE_FOR_ADDING_TAX: str
    AMPAY_ADDING_TAX_RATE: int
    MESSAGE_BROKER_OCPP_QUEUE_PREFIX: str
    OCPP_REMOTESTART_IDTAG_PREFIX: str
    AMPAY_RECEIPT_BASE_URL: str
    CITRINEOS_MESSAGE_API_URL: str
    CITRINEOS_DATA_API_URL: str
    CITRINEOS_SCAN_AND_CHARGE: bool
    CITRINEOS_DIRECTUS_URL: str
    CITRINEOS_DIRECTUS_LOGIN_EMAIL: str
    CITRINEOS_DIRECTUS_LOGIN_PASSWORD: str
    CITRINEOS_DIRECTUS_QR_CODE_FOLDER: str
    CLIENT_URL: str
    # Shared secret for the service-to-service catalog sync API
    # (api/endpoints/catalog.py). Empty => the write endpoints fail closed (503)
    # so the catalog can never be mutated by an unauthenticated caller.
    PAYMENT_CATALOG_SYNC_SECRET: str = ""

    # IANA timezone for the RCD zone_offset_req push (integrations/
    # rcd_vendor.py). Empty (default) => no push: the AC fw V72.78 stores the
    # offset but never renders it -- display time comes from the core's
    # RCD local-currentTime shim instead (OCPP16_RCD_LOCAL_TIME_TZ on
    # citrine). Set only for firmwares verified to render the offset.
    CHARGER_DISPLAY_TIMEZONE: str = ""

    # Pay-by-phone IVR (api/endpoints/ivr.py), driven by Twilio Programmable
    # Voice + <Pay>. Empty TWILIO_AUTH_TOKEN => every IVR endpoint fails
    # closed (503) and the feature is off; card data never touches this
    # service either way (Twilio captures DTMF digits and tokenizes straight
    # into Stripe).
    TWILIO_AUTH_TOKEN: str = ""
    # Public origin Twilio calls: scheme + host only, no path (e.g.
    # https://pay.example.com). Twilio signs the URL it requested, and this
    # service sits behind nginx, so signature validation rebuilds the signed
    # URL as this origin + the request path/query. Empty => validate against
    # the URL as received (direct-exposure dev).
    IVR_PUBLIC_BASE_URL: str = ""
    # Twilio Pay connector to tokenize with (Console: Voice -> Pay
    # Connectors) for operators on the PLATFORM Stripe account. Operators
    # with a real Connect account use the connector named after their
    # acct_... id (convention: one connector per connected account, so the
    # token lands on the account that will be charged). Empty => Twilio's
    # default connector.
    TWILIO_PAY_CONNECTOR: str = ""
    # Spoken as the human-assistance option ("press 0") and on failures.
    # Empty => the option is not offered.
    IVR_SUPPORT_PHONE: str = ""
    # Network name spoken in the greeting.
    IVR_NETWORK_NAME: str = "Ivora"

    # When true, settlement bills any cost above the captured hold as a second
    # off-session charge on the saved card (the "overage" charge). Requires the
    # checkout to have saved the card (web-portal flow). Off => cap-at-hold.
    OVERAGE_CHARGE_ENABLED: bool = True

    # Payment must be made BEFORE charging starts. Chargers are provisioned
    # with TxStartPoint=Authorized (plug-in only occupies the connector; the
    # driver pays via the standing QR and the resulting RequestStartTransaction
    # begins the session). This flag is the CSMS-side safety net: if a
    # mis-provisioned charger starts an unauthorized session anyway, it is
    # stopped immediately instead of charging for free. False restores the
    # legacy pay-while-charging flow (transaction QR + PaymentLink).
    SCAN_AND_CHARGE_REQUIRE_PREPAYMENT: bool = True

    # Stuck-session reaper (tasks/background.py): settle checkouts whose
    # session has had no end packet for REAPER_STALE_HOURS, using the last
    # packet the CSMS saw as the end time. Card-not-present holds expire after
    # ~7 days, so keep the threshold comfortably below that.
    REAPER_ENABLED: bool = True
    REAPER_STALE_HOURS: int = 48
    REAPER_INTERVAL_MINUTES: int = 30
    # Close core OCPP Transactions still flagged isActive after their station
    # has been offline this long: the charger died mid-session (or was
    # replaced) and the closing StopTransaction is never coming, so the
    # operator UI shows the session as "Active" forever. Runs on the reaper
    # cadence; 0 disables.
    JANITOR_OFFLINE_HOURS: int = 12

    # Receipt email (utils/receipt_email.py): the itemized receipt sent to the
    # driver's Stripe Checkout email after settlement. Transport is Resend's
    # HTTP API when RESEND_API_KEY is set, else SMTP when SMTP_HOST is set;
    # SMTP_FROM is the sender for both (the domain must be verified in Resend
    # when using the API). Neither configured => feature off. Mail failures
    # are logged, never raised -- settlement must not depend on the mailer.
    RESEND_API_KEY: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_STARTTLS: bool = True

    # Offline/Faulted charger alerting (tasks/background.py). Empty URL =>
    # disabled. The payload is Slack-compatible: {"text": "..."}.
    ALERT_WEBHOOK_URL: str = ""
    ALERT_OFFLINE_MINUTES: int = 10
    ALERT_INTERVAL_MINUTES: int = 5

    # Dev-only convenience: when AUTO_SEED=true the startup hook in main.py seeds
    # one catalog chain from the SEED_* values below (same upsert as seed.py and
    # the /catalog/sync API). Leave off in any shared/prod environment -- the
    # operator-ui onboarding flow is the real path.
    AUTO_SEED: bool = False
    SEED_STATION_ID: str = "cp002"
    SEED_TENANT_ID: str = "1"
    SEED_OCPP_EVSE_ID: int = 1
    SEED_EVSE_ID: str = "cp002-1"
    SEED_LOCATION_ID: str = "loc-001"
    SEED_OPERATOR_NAME: str = "Test Operator"
    SEED_STRIPE_ACCOUNT_ID: str = "platform"
    SEED_CURRENCY: str = "usd"
    SEED_AUTHORIZATION_AMOUNT: float = 25.0

    """
    Map environment variables to class fields according to these rules:
      - Field won't be parsed unless it has a type annotation
      - Field will be skipped if not in all caps
      - Class field and environment variable name are the same
    """

    def __init__(self, env):
        ENV_FILE = ".env"
        if env.get("CONFIG_PATH") is not None:
            ENV_FILE = env.get("CONFIG_PATH")
        load_dotenv(dotenv_path=ENV_FILE)

        for field in self.__annotations__:
            if not field.isupper():
                continue

            # Raise AppConfigError if required field not supplied
            default_value = getattr(self, field, None)
            if default_value is None and env.get(field) is None:
                raise AppConfigError("The {} field is required".format(field))

            # Cast env var value to expected type and raise AppConfigError on failure
            try:
                var_type = get_type_hints(AppConfig)[field]
                if var_type is bool:
                    value = _parse_bool(env.get(field, default_value))
                else:
                    value = var_type(env.get(field, default_value))

                self.__setattr__(field, value)
            except ValueError:
                raise AppConfigError(
                    'Unable to cast value of "{}" to type "{}" for "{}" field'.format(
                        env[field], var_type, field
                    )
                )

    def __repr__(self):
        return str(self.__dict__)


# Expose Config object for app to import


Config = AppConfig(os.environ)
