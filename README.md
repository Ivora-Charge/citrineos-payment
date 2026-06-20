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