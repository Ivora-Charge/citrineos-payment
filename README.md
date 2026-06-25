# Configuration
Main important config keys which can be set via ENV Variables:

## CitrineOS MESSAGE API URL - NO Trailing Slash
CITRINEOS_MESSAGE_API_URL="http://localhost:8080/ocpp"

## CitrineOS DATA API URL - NO Trailing Slash
CITRINEOS_DATA_API_URL="http://localhost:8080/data"

## CitrineOS SCAN AND CHARGE - enable/disable feature
CITRINEOS_SCAN_AND_CHARGE="true"

## Url for CitrineOS Directus instance - (required for Scan and Charge)
CITRINEOS_DIRECTUS_URL="http://localhost:8055"

## Login Email for CitrineOS Directus instance - (required for Scan and Charge)
CITRINEOS_DIRECTUS_LOGIN_EMAIL="admin@CitrineOS.com"

## Login Password for CitrineOS Directus instance - (required for Scan and Charge)
CITRINEOS_DIRECTUS_LOGIN_PASSWORD="CitrineOS!"

## CitrineOS Directus QR Code folder - (required for Scan and Charge)
CITRINEOS_DIRECTUS_QR_CODE_FOLDER="put folder id here"

## URL which will be used by the frontend application
CLIENT_URL="http://localhost:9010"

# Message Broker (RabbitMQ)
## Protocol of the Message Broker [amqp / amqps] (required)
MESSAGE_BROKER_SSL_ACTIVE=False

## Host of the Message Broker (required)
MESSAGE_BROKER_HOST="127.0.0.1"

## Port of the Message Broker (required)
MESSAGE_BROKER_PORT=5672

## User of the Message Broker (required)
MESSAGE_BROKER_USER="guest"

## Password of the Message Broker (required)
MESSAGE_BROKER_PASSWORD="guest"

## Vhost of the Message Broker (required)
MESSAGE_BROKER_VHOST="/"

## Exchange type to be used for the Message Broker (deafult: topic)
MESSAGE_BROKER_EXCHANGE_TYPE="headers"

## Exchange name to be used for the Message Broker (required)
MESSAGE_BROKER_EXCHANGE_NAME="citrineos"

## The name of the queue where this service will listen for events to be processed, e.g. when a TransactionEvent was received. (required)
MESSAGE_BROKER_EVENT_CONSUMER_QUEUE_NAME="paymentService"

# Webserver Settings (api used by the frontend)
## Host of the web server (required)
WEBSERVER_HOST="0.0.0.0"

## Port of the web server (required)
WEBSERVER_PORT=9010

## Path which will be used as web routes prefix (e.g. "/path") [""]
WEBSERVER_PATH="/api"

## Database settings (required)
DB_HOST="127.0.0.1"
DB_PORT=5432
DB_DATABASE="citrine"
DB_USER="citrine"
DB_PASSWORD="citrine"
DB_TABLE_PREFIX="payment_"

# Stripe settings
## Stripe API Key (required)
(container contains Stackbox' test api key)
STRIPE_API_KEY="sk_test_some-stripe-api-key"

## Stripe endpoint secret for receiving webhooks from Stripe for endpoint-type "Account" (required)
(Webhook needs to be configured with stripe and given secret needs to be used)
STRIPE_ENDPOINT_SECRET_ACCOUNT="whsec_some-stripe-signing-secret"

## Stripe endpoint secret for receiving webhooks from Stripe for endpoint-type "Connect" (required)
(Webhook needs to be configured with stripe and given secret needs to be used)
STRIPE_ENDPOINT_SECRET_CONNECT="whsec_some-stripe-signing-secret"

# Development Setup

## Quick start (full stack)

`./setup_payment_stack.sh` brings up the entire payment stack from a clean
environment in a single run and then starts the service. It is idempotent —
re-running skips work that is already done.

```bash
./setup_payment_stack.sh
```

When it finishes, the service is running at http://localhost:9010 (logs in
`payment.log`, pid in `payment.pid`; stop it with `kill $(cat payment.pid)`).

### What it does

1. **citrineos-core deps** — checks Postgres (5432), RabbitMQ (5672) and the
   CitrineOS API (8080). If they are down it starts citrineos-core from the
   sibling repo's `docker-compose.local.yml` and waits for them.
2. **Directus** — brings up the QR-code host on `:8055`
   (`docker-compose.directus.yml`, sqlite-backed) and waits for it to be healthy.
3. **QR-code folder** — seeds the folder id from `CITRINEOS_DIRECTUS_QR_CODE_FOLDER`
   into Directus so uploads have a home.
4. **Python venv** — creates `.venv` (Python 3.10) and installs `requirements.txt`
   (and `dev-requirements.txt`).
5. **Frontend** — builds `frontend/build` inside a throwaway `node:20` container,
   so no Node install is required on the host.
6. **Launch** — starts the FastAPI app on `:9010` and verifies `/health_check`.

### Prerequisites

- Docker (your user must be in the `docker` group — the script re-execs via
  `sg docker` if the group isn't active in your shell yet).
- `python3.10` on `PATH`.
- A sibling `../citrineos-core` checkout (only needed if the core deps aren't
  already running).
- A `.env` file (the script copies `.env.example` if one is missing — fill in
  real Stripe keys and any other secrets).

### Toggles

```bash
FORCE_FRONTEND=1 ./setup_payment_stack.sh   # rebuild the frontend even if frontend/build exists
NO_START=1 ./setup_payment_stack.sh         # set everything up but don't launch the app
```

### Manual step: public QR assets

Boot and QR-code generation work out of the box, but for QR images to be served
publicly you must grant **public read access** to the QR-code folder's assets in
Directus (Settings → Access Policies). The script prints a reminder about this.

## Manual setup

To set up only the Python environment, run:

```bash
./deploy_local.sh
```

## Catalog sync API

Scan-and-charge and web checkout need an Operator → Location → Tariff → EVSE →
Connector chain in the `payment_*` tables. Historically this was created by
running [`seed.py`](#manual-catalog-seed) by hand. The catalog sync API lets a
trusted backend (e.g. the operator-ui onboarding flow) create/update that chain
over HTTP instead.

Set a shared secret in `.env`:

```
PAYMENT_CATALOG_SYNC_SECRET="changeme-dev-secret"
```

If the secret is empty the write endpoints are disabled and return `503` — the
catalog is never writable anonymously. Every call must send the secret in the
`X-Catalog-Sync-Secret` header (wrong value → `401`).

| Method | Path                       | Purpose                                              |
| ------ | -------------------------- | ---------------------------------------------------- |
| POST   | `/api/catalog/sync`        | Upsert one operator → location → tariff → evse → connector chain |
| GET    | `/api/catalog/status`      | Whether a catalog row exists (`?evse_id=` or `?station_id=&tenant_id=`) |
| POST   | `/api/catalog/sync-station`| Phase 3 stub — pull a station from the CitrineOS data API (`501`) |

Sync is **idempotent**: rows are matched on their natural keys (`operator.name`,
`location.location_id`, `evse.evse_id`, `connector.connector_id`) and updated in
place, so re-syncing never creates duplicates. The EVSE business key follows the
convention `evse_id = {station_id}-{ocpp_evse_id}` (e.g. `cp002-1`).

Tariff and connector fields are optional and fall back to the seed defaults, so a
minimal payload still produces a working row:

```bash
curl -X POST http://localhost:9010/api/catalog/sync \
  -H "X-Catalog-Sync-Secret: changeme-dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
        "operator_name": "Test Operator",
        "stripe_account_id": "platform",
        "location_id": "loc-001",
        "address": "1 Test St", "postal_code": "00000",
        "city": "Testville", "state": "TS", "country": "USA",
        "station_id": "cp002", "tenant_id": "1",
        "ocpp_evse_id": 1, "evse_id": "cp002-1",
        "currency": "usd", "price_kwh": 0.30, "authorization_amount": 25
      }'

# verify
curl http://localhost:9010/api/evses/cp002-1
```

`stripe_account_id` may be a real Connect account (`acct_...`) or, in local dev,
any non-`acct_` value (e.g. `"platform"`) to charge on the platform account — see
`utils/utils.py:stripe_account_kwargs`.

<a id="manual-catalog-seed"></a>
The same logic is exposed as a CLI. `seed.py` is now a thin wrapper over
`catalog/sync.py:upsert_payment_catalog`:

```bash
python seed.py --station-id cp002 --tenant-id 1 --stripe-account-id platform \
               --currency usd --authorization-amount 25
```

### Dev auto-seed on startup

For local development you can have the service seed one catalog chain on boot
instead of running `seed.py` or calling the API. Set `AUTO_SEED=true` plus the
`SEED_*` values (see [`.env.example`](.env.example)); the startup hook in
`main.py` calls the same idempotent `upsert_payment_catalog`, so it is safe to
re-run on every boot. Keep `AUTO_SEED=false` in any shared/prod environment —
the operator-ui onboarding flow is the real path to populate the catalog there.

```bash
AUTO_SEED=true SEED_STATION_ID=cp002 SEED_TENANT_ID=1 \
SEED_STRIPE_ACCOUNT_ID=platform <run the service>
```

> The catalog sync API, `seed.py`, and `AUTO_SEED` all share
> `catalog/sync.py:upsert_payment_catalog`, so they stay in lockstep and are
> mutually backward compatible.

## Tests

To execute the tests, run the following command from the root directory:
```bash
python -m unittest
```

## Code Style

We use [Ruff](https://docs.astral.sh/ruff/) to lint and format our code.

To run the formatter, run the following command:
```bash
ruff format
```
To run the linter, run the following command:
```bash
ruff check
```