"""Platform commission contracts: isolation, snapshots, capture and overage."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text
from pydantic import ValidationError

from tests.api.test_free_checkout import engine, TestSession, seed
from db.init_db import Base, Checkout, TenantPlatformFee
from api.endpoints.checkouts import create_checkout
from api.endpoints.connect import PlatformFeeRequest
from schemas.checkouts import CheckoutCreate
from integrations.integration import OcppIntegration
from config import Config
from utils.platform_fees import checkout_rate, tenant_rate, fee_amount, fee_kwargs


class PlatformFeeTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db = TestSession()
        self.evse = seed(self.db)

    def tearDown(self):
        self.db.close()

    def test_default_override_zero_and_tenant_isolation(self):
        self.assertEqual(tenant_rate(self.db, 7), 1000)
        self.db.add(TenantPlatformFee(tenant_id=7, basis_points=300))
        self.db.commit()
        self.assertEqual(tenant_rate(self.db, 7), 300)
        self.assertEqual(tenant_rate(self.db, 8), 1000)
        self.db.query(TenantPlatformFee).first().basis_points = 0
        self.db.commit()
        self.assertEqual(tenant_rate(self.db, 7), 0)
        self.assertEqual(checkout_rate(self.db, self.evse, 'platform'), 0)

    def test_integer_rounding_and_invalid_rates(self):
        self.assertEqual(fee_amount(10000, 1000), 1000)
        self.assertEqual(fee_amount(10000, 300), 300)
        self.assertEqual(fee_amount(105, 1000), 11)
        self.assertEqual(fee_amount(1, 10000), 1)
        self.assertEqual(fee_amount(0, 1000), 0)
        self.assertEqual(fee_kwargs(1000, None, 'acct_1'), {})
        self.assertEqual(fee_kwargs(1000, 1000, 'platform'), {})
        self.assertEqual(fee_kwargs(1000, 0, 'acct_1'), {'application_fee_amount': 0})
        for bad in [-1, 10001, 3.5, True, '300', None]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PlatformFeeRequest(basis_points=bad)

    def test_web_checkout_snapshots_negotiated_rate(self):
        rate = TenantPlatformFee(tenant_id=7, basis_points=300)
        self.db.add(rate)
        self.db.commit()
        with patch('api.endpoints.checkouts.stripe.checkout.Session.create', return_value=SimpleNamespace(payment_intent='pi_1', url='https://example.com')):
            result = create_checkout(CheckoutCreate(evse_id=self.evse.evse_id, success_url='https://example.com', cancel_url='https://example.com'), self.db)
        rate.basis_points = 1000
        self.db.commit()
        self.assertEqual(self.db.query(Checkout).filter_by(id=result.id).one().platform_fee_bps, 300)

    def settle(self, bps=300, total=1500, account='acct_1'):
        self.evse.location.operator.stripe_account_id = account
        checkout = Checkout(connector_id=self.evse.connectors[0].id,
            tariff_id=self.evse.connectors[0].tariff_id, platform_fee_bps=bps,
            payment_intent_id='pi_hold', authorization_amount=1000)
        self.db.add(checkout)
        self.db.commit()
        pricing = SimpleNamespace(total_due=total, currency='USD')
        hold = SimpleNamespace(status='succeeded', customer='cus_1', payment_method='pm_1')
        checkout_id = checkout.id
        def get_db():
            with TestSession() as db:
                yield db
        with patch('integrations.integration.get_db', get_db), \
             patch('integrations.integration.generate_pricing', return_value=pricing), \
             patch('integrations.integration.send_receipt_email'), \
             patch.object(Config, 'OVERAGE_CHARGE_ENABLED', True), \
             patch('integrations.integration.stripe.PaymentIntent.retrieve', return_value=SimpleNamespace(status='requires_capture')), \
             patch('integrations.integration.stripe.PaymentIntent.capture', return_value=hold) as capture, \
             patch('integrations.integration.stripe.PaymentIntent.cancel', return_value=SimpleNamespace(status='canceled')) as cancel, \
             patch('integrations.integration.stripe.PaymentIntent.create', return_value=SimpleNamespace(status='succeeded', id='pi_over')) as overage:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(OcppIntegration().capture_payment_transaction(checkout_id=checkout_id))
                loop.run_until_complete(OcppIntegration().capture_payment_transaction(checkout_id=checkout_id))
            finally:
                loop.close()
        self.db.refresh(checkout)
        return checkout, capture, overage, cancel

    def test_partial_capture_and_overage_take_fee_only_on_collected_amount(self):
        ck, capture, overage, _ = self.settle()
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.kwargs['amount_to_capture'], 1000)
        self.assertEqual(capture.call_args.kwargs['application_fee_amount'], 30)
        self.assertEqual(capture.call_args.kwargs['stripe_account'], 'acct_1')
        self.assertEqual(overage.call_args.kwargs['application_fee_amount'], 15)
        self.assertEqual(ck.platform_fee_amount, 30)
        self.assertEqual(ck.overage_platform_fee_amount, 15)

    def test_actual_capture_smaller_than_hold(self):
        ck, capture, overage, _ = self.settle(bps=1000, total=250)
        self.assertEqual(capture.call_args.kwargs['application_fee_amount'], 25)
        overage.assert_not_called()

    def test_legacy_checkout_keeps_original_no_fee_terms(self):
        _, capture, overage, _ = self.settle(bps=None)
        self.assertNotIn('application_fee_amount', capture.call_args.kwargs)
        self.assertNotIn('application_fee_amount', overage.call_args.kwargs)

    def test_platform_account_omits_connect_fee(self):
        _, capture, overage, _ = self.settle(account='platform')
        self.assertNotIn('application_fee_amount', capture.call_args.kwargs)
        self.assertNotIn('application_fee_amount', overage.call_args.kwargs)

    def test_tiny_session_cancels_without_fee(self):
        _, capture, overage, cancel = self.settle(total=20)
        capture.assert_not_called()
        overage.assert_not_called()
        cancel.assert_called_once()


class PlatformFeeApiTests(unittest.TestCase):
    def setUp(self):
        from tests.api.test_connect import engine as connect_engine, client, TEST_SECRET
        self.engine, self.client, self.secret = connect_engine, client, TEST_SECRET
        Config.PAYMENT_CATALOG_SYNC_SECRET = TEST_SECRET
        TenantPlatformFee.__table__.drop(self.engine, checkfirst=True)
        TenantPlatformFee.__table__.create(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text('DROP TABLE IF EXISTS "Tenants"'))
            conn.execute(text('CREATE TABLE "Tenants" (id INTEGER PRIMARY KEY, "stripeAccountId" TEXT)'))
            conn.execute(text('INSERT INTO "Tenants" VALUES (7, NULL)'))

    def test_secret_required_for_reads_and_writes(self):
        for method in ['get', 'put']:
            response = getattr(self.client, method)('/api/connect/platform-fee?tenant_id=7',
                **({'json': {'basis_points': 300}} if method == 'put' else {}))
            self.assertIn(response.status_code, (401, 403))

    def test_fee_crud_and_validation(self):
        headers = {'X-Catalog-Sync-Secret': self.secret}
        url = '/api/connect/platform-fee?tenant_id=7'
        self.assertEqual(self.client.get(url, headers=headers).json()['basis_points'], 1000)
        for rate in [300, 0, 1000]:
            response = self.client.put(url, headers=headers, json={'basis_points': rate})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.get(url, headers=headers).json()['basis_points'], rate)
        self.assertEqual(self.client.put(url, headers=headers, json={'basis_points': 10001}).status_code, 422)
        self.assertEqual(self.client.put('/api/connect/platform-fee?tenant_id=999', headers=headers,
            json={'basis_points': 300}).status_code, 404)
