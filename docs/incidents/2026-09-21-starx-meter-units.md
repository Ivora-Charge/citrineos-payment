# Starx meter-unit overcharge — September 21, 2026

Fixed in production at September 22, 2026, 00:32 UTC (September 21 in US
timezones). Verified against production Postgres, Stripe, checkout HTTP responses,
the deployed module hash, container health, and payment event-consumer logs at
00:33 UTC.

## Cause

The hold-recovery release introduced a second Wh-to-kWh conversion in
`utils/payment_lifecycle.py:reconcile_core_transaction`. Core
`Transactions.meterStart` is already kWh; the raw OCPP 1.6 `meterStart` is Wh.
Recovery divided the core value by 1,000 before reconstructing the last meter
reading. The next normal meter packet then added most of the charger's lifetime
energy to this session's usage.

For station `R132739260301008`, transaction 30 (checkout 84), the raw start/stop
readings were 328828 / 335769 Wh: 6.941 kWh delivered. The corrupted checkout
recorded 335.440172 kWh and charged $117.40 ($25 capture + $92.40 overage).
Stripe confirmed both charges had already been fully refunded before this fix.
The current tariff is $0.35/kWh without additional fees or tax. Existing pricing
truncates fractional cents, so the corrected calculation is $2.42, rather than
the $2.43 rounded estimate initially given during investigation.

## Repair and settlement

- Removed the extra division. Core start and total energy now remain in kWh.
- Paused only the payment service to prevent a second overcharge; the core
  service continued receiving charging data.
- Repaired checkout 84 to 6.941 kWh. Preserved its original captured amounts,
  payment IDs, settlement marker, and completed Stripe refunds; it was not
  charged again.
- Repaired transaction 31 (checkout 88), also affected but not yet captured.
  It completed while payment was paused, delivering 11.257 kWh. The restarted
  service captured $3.93, released the remaining authorization, and created no
  overage payment. Stripe reports amount received 393 cents and zero capturable.
- Both repaired rows now agree with core totals and raw starting readings.
  A historical scan of matched checkout/core records found no remaining
  discrepancies between session energy and final register minus starting register.

The repair transaction checked the exact erroneous baseline offset and station/
tenant/session identities before updating. Stripe calls in the repair script
were read-only. Normal application settlement handled checkout 88 after restart.

## Verification

- Added regressions exercising the PostgreSQL-only reconciliation branch,
  followed by normal OCPP meter processing and settlement with Stripe mocked.
  They reproduced the $117.40 bill before the fix and assert 242 cents with no
  overage afterward. Repeated recovery and lagging core snapshots are covered.
- 185 repository backend tests passed.
- 175 applicable tests passed against the exact production code bundle. The
  undeployed commission test module and three commission-only assertions were
  excluded from that temporary validation copy; other test steps were retained.
- Ruff and whitespace checks passed for the change.
- Production checkout APIs return 200 and correct energy/pricing for both rows.
- The payment container is healthy, subscribed to charging events, and consumed
  the queued events without repeating settlement or corrupting corrected totals.

## Deployment and recovery evidence

Only `utils/payment_lifecycle.py` changed in the production image. Existing
dependencies, frontend assets, and pricing/commission behavior were preserved.
The production source checkout was updated to match the image.

- Image: `citrineos-payment:meter-units-20260921`
- Image ID: `60c257c551a6570deb278d1cd6dfd5be4ea84e9c961aad8cebf8e3b68ab2f680`
- Module SHA-256: `bd1433ca3b61099fe6f37a94db7376972ceffd5b2b49963695122d80b81de1dc`
- Previous image: `citrineos-payment:before-meter-units-20260921`
- Production backups and before/after repair records:
  `/home/ubuntu/csms/backups/payment-meter-units-20260921`

The previous image contains this billing defect. If reverting code, keep payment
processing stopped until the unit correction is restored. Do not restore the
corrupted meter data or undo the completed refunds/settlement.
