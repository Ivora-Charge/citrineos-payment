#!/usr/bin/env bash
# Cron watchdog: restart the payment uvicorn if it is not running OR not
# responding. Guarded twice so it can NEVER start a second instance — duplicate
# uvicorns run init_db DDL concurrently and wedge the whole payment_* schema
# behind an ACCESS EXCLUSIVE lock.
exec 9>/tmp/payment_watchdog.lock
flock -n 9 || exit 0

cd /home/mingcan/csms/citrineos-payment || exit 1

# Anchored to the venv python executable so shells/wrappers whose command
# line merely *contains* "uvicorn main:app" don't mask a dead service.
PGREP_PATTERN='^/home/mingcan/csms/citrineos-payment/\.venv/bin/python .venv/bin/uvicorn'

if pgrep -f "$PGREP_PATTERN" >/dev/null; then
  # Process exists — but a blocked event loop (e.g. a sync HTTP call that
  # hangs) leaves a zombie that pgrep can't tell apart: the 2026-07-08 trial
  # lost the AMQP consumer for 74 minutes this way. A blocked loop can't
  # answer HTTP either, so probe /health_check; on failure kill and restart.
  if curl -sf --max-time 10 http://localhost:9010/health_check >/dev/null; then
    exit 0
  fi
  echo "[watchdog] $(date -u +%FT%TZ) payment service unresponsive (health probe failed), killing" >> payment.log
  pkill -9 -f "$PGREP_PATTERN"
  sleep 2
fi

echo "[watchdog] $(date -u +%FT%TZ) payment service down, restarting" >> payment.log
# 9>&- : do NOT leak the flock fd into the service, or the running uvicorn
# holds the watchdog lock forever and every later watchdog run no-ops at
# `flock -n` (which is how the unresponsive-service probe silently never ran).
setsid .venv/bin/uvicorn main:app --host 0.0.0.0 --port 9010 >> payment.log 2>&1 9>&- &
sleep 5
ps -eo pid,args \
  | awk '$2 ~ /^\/home\/mingcan\/csms\/citrineos-payment\/\.venv\/bin\/python/ {print $1}' \
  | head -1 > payment.pid
