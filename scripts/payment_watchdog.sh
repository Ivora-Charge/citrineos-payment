#!/usr/bin/env bash
# Cron watchdog: restart the payment uvicorn if (and only if) it is not
# running. Guarded twice so it can NEVER start a second instance — duplicate
# uvicorns run init_db DDL concurrently and wedge the whole payment_* schema
# behind an ACCESS EXCLUSIVE lock.
exec 9>/tmp/payment_watchdog.lock
flock -n 9 || exit 0

cd /home/mingcan/csms/citrineos-payment || exit 1

# Anchored to the venv python executable so shells/wrappers whose command
# line merely *contains* "uvicorn main:app" don't mask a dead service.
if pgrep -f '^/home/mingcan/csms/citrineos-payment/\.venv/bin/python .venv/bin/uvicorn' >/dev/null; then
  exit 0
fi

echo "[watchdog] $(date -u +%FT%TZ) payment service down, restarting" >> payment.log
setsid .venv/bin/uvicorn main:app --host 0.0.0.0 --port 9010 >> payment.log 2>&1 &
sleep 5
ps -eo pid,args \
  | awk '$2 ~ /^\/home\/mingcan\/csms\/citrineos-payment\/\.venv\/bin\/python/ {print $1}' \
  | head -1 > payment.pid
