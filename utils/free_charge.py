"""Admin free charging: verify the per-charger password platform-api set.

The hash arrives with the catalog sync (platform-api/free_charge.py writes
it; keep the two files in step):
    pbkdf2_sha256$<iterations>$<salt b64>$<digest b64>
Verification runs on the checkout hot path and must not call any other
service, so the hash lives on payment_evses and only the standard library
is used.

A small in-memory throttle slows password guessing per EVSE. It is per
process and resets on restart, which is enough for a password a host picks
for their own charger; it is not an account lockout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time

ALGORITHM = "pbkdf2_sha256"

# After MAX_FAILURES wrong passwords within WINDOW_SECONDS, an EVSE refuses
# further attempts until the window has passed.
MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        rounds = int(iterations)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(digest, expected)


def _recent(key: str, now: float) -> list[float]:
    stamps = [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]
    if stamps:
        _failures[key] = stamps
    else:
        _failures.pop(key, None)
    return stamps


def is_locked(key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _lock:
        return len(_recent(key, now)) >= MAX_FAILURES


def record_failure(key: str, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _lock:
        _failures[key] = _recent(key, now) + [now]


def clear_failures(key: str) -> None:
    with _lock:
        _failures.pop(key, None)
