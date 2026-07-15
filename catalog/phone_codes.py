"""Numeric pay-by-phone codes for EVSEs.

The IVR (api/endpoints/ivr.py) identifies a charger by a code the caller keys
in, so codes are digits-only and unique across the whole network -- a caller
has no tenant or location context. Codes are 6 digits, randomly assigned (not
sequential, so a caller can't walk the keyspace), never reassigned while the
EVSE exists, and printed on the charger signage next to the toll-free number.
"""

from logging import info
from secrets import randbelow

from db.init_db import Evse

PHONE_CODE_MIN = 100000  # 6 digits, no leading zero (unambiguous on signage)
PHONE_CODE_SPAN = 900000


def _random_code() -> str:
    return str(PHONE_CODE_MIN + randbelow(PHONE_CODE_SPAN))


def ensure_phone_code(db, evse: Evse) -> str:
    """Assign a network-unique phone code to ``evse`` if it lacks one.

    Flushes without committing (same contract as catalog.sync: the caller owns
    the transaction). The unique index on the column is the last line of
    defense against a concurrent assignment of the same code.
    """
    if evse.phone_code:
        return evse.phone_code
    for _ in range(50):
        code = _random_code()
        taken = db.query(Evse).filter(Evse.phone_code == code).first()
        if taken is None:
            evse.phone_code = code
            db.add(evse)
            db.flush()
            return code
    # 50 straight collisions means the 900k keyspace is effectively full.
    raise RuntimeError("could not allocate a unique phone code")


def backfill_phone_codes(db) -> int:
    """Give every code-less EVSE a phone code. Idempotent; returns how many
    were assigned. Runs at service startup so EVSEs that predate this feature
    (or were created while it was off) become phone-payable without manual
    work."""
    assigned = 0
    for evse in db.query(Evse).filter(Evse.phone_code.is_(None)).all():
        ensure_phone_code(db, evse)
        assigned += 1
    if assigned:
        info(" [phone_codes] assigned %d new EVSE phone code(s)", assigned)
    return assigned
