# Starx uncaptured authorizations — September 21, 2026

Production remediation completed at 18:55 UTC; Stripe reconciliation verified at
18:56 UTC. Starx had 18 uncaptured $25 authorizations ($450). All 18 were canceled
on its connected Stripe account at the user's request. This included checkout 73,
whose previously uncollected $2.54 usage was waived. Previously captured payments
were not refunded. No uncaptured Stripe payments or unsettled paid checkouts
remained for Starx after verification.

## Causes

- Seventeen remote starts were accepted but never produced a charging transaction.
  The charger later returned from Preparing to Available without a fault code;
  the physical reason for the missing starts was not established.
- The reaper only considered started, unfinished sessions. It missed both pending
  starts and completed sessions whose capture had failed.
- Cancellation omitted the connected Stripe account and returned "No such
  payment_intent" for these direct charges.
- Manually acquired SQLAlchemy sessions leaked connections. Checkout 73's capture
  failed when the 20-connection pool plus 30 overflow connections was exhausted.

## Repair

Cancellation persists its intent before calling Stripe, snapshots the account,
revokes the payment authorization token, and retries failed releases. Late payment
webhooks cannot restart canceled checkouts. Rejected commands and failed starts
release immediately; a separate worker checks for five minutes of inactivity
every five seconds, independently of the browser. Inactive sessions with usage
capture that usage and release the unused hold; sessions without usage release
the entire hold. Stop requests continue after settlement until the charger ends
the transaction. Completed captures are retried. Runtime DB sessions now close
on all exits, and capture reuses its existing session for pricing.

The mobile page exposes Cancel start before charging, Stop charging while active,
and distinguishes release pending, canceled, settling, and finished states.
Stripe cancellation releases the authorization; bank displays may update later.

## Verification

- Reproduced the original development failure with a real Stripe test hold and
  local OCPP 1.6 simulator: accepted command, no StartTransaction, cancel HTTP 409.
- Rejected start: hold canceled in about 1.4 seconds.
- Preparing → Available: hold canceled in about 2 seconds.
- Browser closed, no start: authorized 18:47:35 UTC, deadline 18:52:35,
  canceled 18:52:39 UTC; Stripe amount capturable became zero.
- Real mobile browser at 390 × 844: prestart cancellation passed. Active stop
  captured $0.90 and released the remainder of the $25 authorization.
- 183 repository backend tests passed; 173 applicable tests passed against the
  exact production code bundle. The production baseline does not yet contain
  the repository's separate commission feature, which was excluded from this
  release and its corresponding fee assertions.
- Production public JavaScript hash matched the tested build; a read-only mobile
  browser check displayed the canceled session without JavaScript errors.
- Production event consumer subscribed successfully. Database checks showed zero
  idle-in-transaction connections after deployment.

## Deployment and rollback

Only the payment service was recreated. Database changes are additive. Existing
production dependencies and pricing behavior were preserved. The commission-free
release was verified by comparing the syntax trees of the original production
modules against the repository baseline with only commission additions removed.
Both production source and built mobile assets were updated.

Production image: `citrineos-payment:recovery-20260921` (`ffc0cda13567`).
Rollback image: `citrineos-payment:before-recovery-20260921` (`028ff66d08c9`).
Production source and checkout backups are under
`/home/ubuntu/csms/backups/payment-recovery-20260921`.

Rollback can retag the previous image as `citrineos-payment:local` and recreate
only `payment` through the existing deployment compose file. The extra columns
can remain. Do not restore old payment data over the completed Stripe releases.
