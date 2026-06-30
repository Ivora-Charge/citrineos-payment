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

## Bill cost above the captured hold as a second off-session "overage" charge on
## the saved card (default false => cap at the hold). See the Developer Guide.
OVERAGE_CHARGE_ENABLED=false

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

# Developer Guide

This service is a FastAPI app that sits between **CitrineOS** (the OCPP CSMS) and
**Stripe**. It owns the payment lifecycle of a charging session: showing a pay
QR, taking the payment, telling CitrineOS to start the charge, metering it from
OCPP events, and settling the money at the end.

## Architecture at a glance

```
  Driver phone                Charger (OCPP)
       │  scan QR / pay             │  StatusNotification / TransactionEvent
       ▼                            ▼
  PayServe (frontend) ──HTTP──►  CitrineOS  ──RabbitMQ──►  payment event consumer
       │                            ▲                          (integrations/citrineos)
       │ /api/*                     │  message API                   │
       ▼                            │  (SetDisplayMessage,           │ updates
   payment API  ──────────────────► │   RequestStartTransaction,     ▼
   (api/endpoints)                  │   DataTransfer, …)         payment_* tables
       │                            └───────────────────────────     │
       ▼                                                              ▼
     Stripe  ◄────────  webhooks (/api/webhooks/stripe)  ◄──── capture / overage
```

Two things drive the service:

1. **The REST API** (`api/`) — called by the PayServe frontend (`evses`,
   `locations`, `tariffs`, `checkouts`) and by trusted backends (`catalog`).
2. **The OCPP event consumer** (`integrations/citrineos/citrineos.py`) — a
   long-running RabbitMQ consumer started from `main.py` that reacts to
   `TransactionEvent` and `StatusNotification` and pushes messages back to
   chargers through CitrineOS's message API.

## Repository layout

| Path | What lives there |
| ---- | ---------------- |
| `main.py` | App bootstrap: sets `stripe.api_key`, runs `init_db()`, attaches `app.ocpp_integration`, starts the event-consumer task, optional `AUTO_SEED`. |
| `api/api.py` | Mounts the routers under `/api`. |
| `api/endpoints/` | `checkouts`, `evses`, `locations`, `tariffs`, `webhooks`, `catalog`. |
| `integrations/integration.py` | Base classes `OcppIntegration` (incl. `capture_payment_transaction`) and `FileIntegration`. |
| `integrations/citrineos/citrineos.py` | `CitrineOSIntegration`: event consumer + handlers, and `send_citrineos_message` to talk to chargers. |
| `integrations/charger_display.py` | **Charger-display adapters** — how each charger family renders the pay QR (extension point). |
| `integrations/directus/directus.py` | Uploads QR images to Directus (a `FileIntegration`). |
| `catalog/sync.py` | `upsert_payment_catalog` — the one source of truth for the `payment_*` catalog chain. |
| `db/init_db.py` | SQLAlchemy models (`payment_*` tables + read-only maps of CitrineOS tables) and `init_db()`. |
| `model/transaction_summary.py`, `utils/utils.py` | Pricing math (`generate_pricing`) and Stripe helpers. |
| `schemas/` | Pydantic request/response models. |
| `frontend/` | PayServe React app (the driver-facing pages). |

## Charging & payment flows

- **Web-portal** — driver opens `/checkout/{evse_id}` → `POST /api/checkouts`
  creates a Stripe **Checkout Session** (a manual-capture hold) → on payment the
  `checkout.session.completed` webhook runs `handle_web_portal`, which authorizes
  and sends `RequestStartTransaction` (the charger *arms* and starts on plug-in).
- **Scan & charge** — an unauthorized plug-in (`TransactionEvent` Started,
  `CablePluggedIn`, no idToken) makes the service create a Stripe **PaymentLink**
  + QR and push it to the charger; after payment `handle_scan_and_charge` arms/
  starts the session. Requires `CITRINEOS_SCAN_AND_CHARGE=true`.
- **Standing "scan to pay" QR** — pushed when a charger goes `Available`
  (`process_status_notification`) and after a catalog sync; cleared when the
  connector is in use. Encodes the static `/checkout/{evse_id}` page.
- **Settlement** — `TransactionEvent` Ended → `process_transaction_ended` →
  `capture_payment_transaction`.

Both checkout flows save the card (`setup_future_usage=off_session`) so settlement
can bill an overage (below).

## Billing model: hold → capture → overage

A session places a **manual-capture authorization hold** of `authorization_amount`
at checkout. At the end, `capture_payment_transaction`:

1. Computes the real cost via `generate_pricing` (energy + time + session fee + tax).
2. Captures it, **capped at the hold** (Stripe can't capture more than authorized).
3. If `OVERAGE_CHARGE_ENABLED` and the cost exceeded the hold, bills the remainder
   as a **second off-session PaymentIntent** on the saved card (`_charge_overage`).
   Off-session declines/SCA are logged, not raised; `overage_payment_intent_id`
   makes it idempotent against duplicate `Ended` events.

Time cost uses the **OCPP packet timestamps** (`transaction_event.timestamp`), not
receive time, so a replayed offline `Ended` still bills the real duration.

## Extension points

### Add support for a new charger family (display adapter)

Different chargers accept the pay QR differently — the standard way is OCPP
`SetDisplayMessage` with a rendered image; Renova ("rcd") devices take a URL over
a vendor `DataTransfer` and render the QR themselves. This is pluggable in
`integrations/charger_display.py`:

```python
class MyChargerDisplayAdapter(ChargerDisplayAdapter):
    async def show_payment_qr(self, ocpp, db, evse, *, payment_url, price, currency):
        ocpp.send_citrineos_message(
            station_id=evse.station_id, tenant_id=evse.tenant_id,
            url_path="configuration/dataTransfer",          # or setDisplayMessage, …
            json_payload={...},
        )
        evse.display_message_id = SHOWN_SENTINEL  # any non-null = "a QR is shown"
        db.add(evse); db.commit()

    async def clear_payment_qr(self, ocpp, db, evse):
        ...
        evse.display_message_id = None
        db.add(evse); db.commit()

ADAPTERS["mycharger"] = MyChargerDisplayAdapter()
```

`charger_type` is **auto-detected** from the charger's BootNotification vendor:
add a `CHARGER_TYPE_BY_VENDOR` entry (e.g. `"RCD" -> "renova"`) and the service
resolves and caches it on first push by reading the CitrineOS
`ChargingStations.chargePointVendor`. A value set by hand
(`UPDATE payment_evses SET charger_type='mycharger' …`) always wins. The adapter
is resolved per-EVSE by `get_display_adapter(evse)`; unset/unknown types fall
back to `"standard"`. `ocpp` (the `CitrineOSIntegration`) gives you
`send_citrineos_message`, `fileIntegration.upload_file`, and the display-message
id helpers. Nothing in the payment flow changes.

### Send an OCPP message to a charger

```python
self.send_citrineos_message(
    station_id=..., tenant_id=...,
    url_path="<citrineos-module>/<camelCaseAction>",   # e.g. evdriver/requestStartTransaction
    json_payload={...},                                # the OCPP request body
)
```

`url_path` is the CitrineOS message-API route: the module that owns the action
plus the action name. Examples in use: `configuration/setDisplayMessage`,
`configuration/clearDisplayMessage`, `configuration/dataTransfer`,
`evdriver/requestStartTransaction`.

### Handle a new OCPP event

Bind the action in `_consume_events` (the `arguments_list` headers binding) and
dispatch it in `process_incoming_event`. The consumer currently binds
`TransactionEvent` and `StatusNotification`.

### Add a database column

There is no Alembic yet, and `create_all()` does **not** add columns to existing
tables. So a new column needs **two** edits in `db/init_db.py`:

1. add the `Column(...)` to the model, and
2. add an idempotent `ALTER TABLE "<table>" ADD COLUMN IF NOT EXISTS ...` in
   `init_db()` (see the `payment_evses` / `payment_checkouts` examples there).

### Add an API endpoint

Create a router in `api/endpoints/` and mount it in `api/api.py`
(`api_router.include_router(..., prefix="/...")`). Service-to-service write
endpoints should require a shared secret like `catalog.py` does.

## Local dev loop

```bash
# run (full stack) — see Development Setup above
./setup_payment_stack.sh

# restart just the app after a code change
kill $(cat payment.pid) 2>/dev/null
./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 9010 > payment.log 2>&1 &

# watch what the service is doing (events, captures, OCPP sends)
tail -f payment.log
```

The service is stateless beyond the DB, so a restart is safe at any time. DB
columns are added by `init_db()` on boot (the idempotent ALTERs above).

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