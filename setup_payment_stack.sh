#!/usr/bin/env bash
#
# setup_payment_stack.sh
# One-shot setup for the citrineos-payment stack from a clean environment.
#
# Brings up everything the payment service needs and then starts it:
#   1. citrineos-core deps (Postgres 5432, RabbitMQ 5672, CitrineOS API 8080) — started if down
#   2. Directus QR-code host on :8055 (sqlite-backed) + the QR-code folder seeded
#   3. Python 3.10 venv with all requirements
#   4. Frontend build (built inside a node:20 container — no host node needed)
#   5. The FastAPI app on :9010, verified via /health_check
#
# Idempotent: safe to re-run. Re-running skips work that is already done.
#
# Useful env toggles:
#   FORCE_FRONTEND=1   rebuild the frontend even if frontend/build already exists
#   NO_START=1         set everything up but do not start the app
#
set -euo pipefail

# --- locate ourselves / sibling repos -------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYMENT_DIR="$SCRIPT_DIR"
CORE_DIR="$(dirname "$SCRIPT_DIR")/citrineos-core"
cd "$PAYMENT_DIR"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- ensure we can talk to docker -----------------------------------------
# If the current shell isn't in the active docker group yet, re-exec under it.
if ! docker info >/dev/null 2>&1; then
  if id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
    warn "docker group not active in this shell — re-executing via 'sg docker'"
    exec sg docker -c "$(printf '%q ' "$BASH_SOURCE" "$@")"
  fi
  die "Cannot access the Docker daemon. Add yourself to the 'docker' group or run with sudo."
fi

# compose v2 ('docker compose') or v1 ('docker-compose')
if docker compose version >/dev/null 2>&1; then
  COMPOSE() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE() { docker-compose "$@"; }
else
  die "Neither 'docker compose' nor 'docker-compose' is installed."
fi

port_open() { (echo > "/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

# --- 1. citrineos-core dependencies ---------------------------------------
log "Checking citrineos-core dependencies (Postgres 5432, RabbitMQ 5672, API 8080)"
if port_open 5432 && port_open 5672 && port_open 8080; then
  ok "core dependencies already reachable"
else
  [ -d "$CORE_DIR" ] || die "core deps are down and citrineos-core not found at $CORE_DIR"
  warn "core deps not reachable — starting citrineos-core (this can take a while on first run)"
  ( cd "$CORE_DIR" && COMPOSE -f docker-compose.local.yml up -d )
  log "Waiting for core dependencies to come up"
  for _ in $(seq 1 60); do
    port_open 5432 && port_open 5672 && port_open 8080 && break
    sleep 5
  done
  port_open 5432 && port_open 5672 && port_open 8080 \
    && ok "core dependencies reachable" \
    || die "core dependencies did not come up — check 'docker ps' in $CORE_DIR"
fi

# --- 2. Directus ----------------------------------------------------------
log "Starting Directus (QR-code host) on :8055"
COMPOSE -f docker-compose.directus.yml up -d

log "Waiting for Directus to become healthy"
for _ in $(seq 1 60); do
  [ "$(curl -fs -o /dev/null -w '%{http_code}' http://localhost:8055/server/health || true)" = "200" ] && break
  sleep 5
done
[ "$(curl -fs -o /dev/null -w '%{http_code}' http://localhost:8055/server/health || true)" = "200" ] \
  || die "Directus did not become healthy — see: docker logs directus"
ok "Directus healthy"

# Pull the admin creds + QR folder id straight from .env so they never drift.
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && warn "created .env from .env.example — fill in real values" || die ".env missing and no .env.example to copy"; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
D_URL="${CITRINEOS_DIRECTUS_URL:-http://localhost:8055}"
D_EMAIL="${CITRINEOS_DIRECTUS_LOGIN_EMAIL:?CITRINEOS_DIRECTUS_LOGIN_EMAIL missing in .env}"
D_PASS="${CITRINEOS_DIRECTUS_LOGIN_PASSWORD:?CITRINEOS_DIRECTUS_LOGIN_PASSWORD missing in .env}"
D_FOLDER="${CITRINEOS_DIRECTUS_QR_CODE_FOLDER:?CITRINEOS_DIRECTUS_QR_CODE_FOLDER missing in .env}"

log "Seeding the QR-code folder ($D_FOLDER) in Directus"
TOKEN="$(curl -fs -X POST "$D_URL/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$D_EMAIL\",\"password\":\"$D_PASS\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["access_token"])')"
[ -n "$TOKEN" ] || die "could not authenticate to Directus with the .env credentials"

if curl -fs -o /dev/null "$D_URL/folders/$D_FOLDER" -H "Authorization: Bearer $TOKEN"; then
  ok "QR-code folder already exists"
else
  curl -fs -X POST "$D_URL/folders" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"$D_FOLDER\",\"name\":\"qr-codes\"}" >/dev/null \
    && ok "QR-code folder created" \
    || warn "could not create QR-code folder — create it manually in Directus with id $D_FOLDER"
  warn "Public read access for QR assets must be granted in Directus (Settings → Access Policies) for the folder to serve images publicly."
fi

# --- 3. Python venv -------------------------------------------------------
log "Setting up Python venv (.venv)"
command -v python3.10 >/dev/null 2>&1 || die "python3.10 not found on PATH"
[ -d .venv ] || python3.10 -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
[ -f dev-requirements.txt ] && ./.venv/bin/python -m pip install --quiet -r dev-requirements.txt
ok "Python dependencies installed"

# --- 4. Frontend build ----------------------------------------------------
if [ -f frontend/build/index.html ] && [ -z "${FORCE_FRONTEND:-}" ]; then
  ok "frontend already built (set FORCE_FRONTEND=1 to rebuild)"
else
  log "Building the frontend inside a node:20 container"
  # DISABLE_ESLINT_PLUGIN: the CRA build's inline eslintConfig still extends the
  # legacy "react-app" preset (not installed); linting is done separately via
  # eslint.config.mjs. CI=false keeps warnings from being treated as errors.
  docker run --rm \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e DISABLE_ESLINT_PLUGIN=true -e CI=false \
    -v "$PAYMENT_DIR/frontend":/app/frontend -w /app/frontend \
    node:20 bash -c "npm install && npm run build"
  [ -f frontend/build/index.html ] || die "frontend build did not produce frontend/build/index.html"
  ok "frontend built"
fi

# --- 5. Start the app -----------------------------------------------------
if [ -n "${NO_START:-}" ]; then
  log "NO_START set — setup complete, not launching the app"
  echo "    Start it with: ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port ${WEBSERVER_PORT:-9010}"
  exit 0
fi

PORT="${WEBSERVER_PORT:-9010}"
if port_open "$PORT"; then
  warn "something is already listening on :$PORT — not starting a second instance"
else
  log "Starting the payment service on :$PORT"
  nohup ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$PORT" \
    > "$PAYMENT_DIR/payment.log" 2>&1 &
  echo "$!" > "$PAYMENT_DIR/payment.pid"
  for _ in $(seq 1 20); do
    [ "$(curl -fs -o /dev/null -w '%{http_code}' "http://localhost:$PORT/health_check" || true)" = "200" ] && break
    sleep 1
  done
fi

if [ "$(curl -fs -o /dev/null -w '%{http_code}' "http://localhost:$PORT/health_check" || true)" = "200" ]; then
  ok "payment service healthy at http://localhost:$PORT"
  log "Done — payment stack is up."
  echo "    App:      http://localhost:$PORT   (logs: payment.log, pid: payment.pid)"
  echo "    Directus: http://localhost:8055"
  echo "    Stop app: kill \$(cat payment.pid)"
else
  die "payment service did not become healthy — check payment.log"
fi
