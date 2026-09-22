"""Development-only fixture: real Stripe test holds and signed local webhooks.

Run from citrineos-payment: .venv/bin/python scripts/payment_recovery_dev.py create no-start
The OCPP fixture must be listening on port 9197. Never accepts production keys/hosts.
"""
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2
import requests
import stripe
from config import Config

ACCOUNT = 'acct_1TzPUUD15WcsvBWd'
STATION = 'payment-recovery-20260921'
API = 'http://127.0.0.1:9010/api'
assert Config.STRIPE_API_KEY.startswith('sk_test_'), 'Refusing live Stripe key'
assert Config.DB_HOST in ('localhost', '127.0.0.1'), 'Refusing remote database'
stripe.api_key = Config.STRIPE_API_KEY
conn = psycopg2.connect(host=Config.DB_HOST,port=Config.DB_PORT,dbname=Config.DB_DATABASE,
                        user=Config.DB_USER,password=Config.DB_PASSWORD)


def signed_webhook(cid, intent):
    event = {'id':f'evt_recovery_{cid}', 'object':'event', 'created':int(time.time()),
             'account':ACCOUNT, 'type':'checkout.session.completed', 'livemode':False,
             'data':{'object':{'id':f'cs_recovery_{cid}','object':'checkout.session',
                     'payment_intent':intent,'amount_total':2500,'metadata':{'checkoutId':str(cid)}}}}
    body=json.dumps(event).encode()
    stamp=str(int(time.time()))
    signature=hmac.new(Config.STRIPE_ENDPOINT_SECRET_CONNECT.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    response=requests.post(API+'/webhooks/stripe', data=body,
        headers={'Stripe-Signature':f't={stamp},v1={signature}','Content-Type':'application/json'},timeout=30)
    response.raise_for_status()


def create(mode):
    requests.post('http://127.0.0.1:9197/?mode='+mode,timeout=5).raise_for_status()
    with conn.cursor() as cur:
        cur.execute('SELECT "tenantId" FROM "ChargingStations" WHERE "ocppConnectionName"=%s',(STATION,))
        tenant=cur.fetchone()[0]
    conn.rollback()
    catalog = requests.post(API+'/catalog/sync',headers={'X-Catalog-Sync-Secret':Config.PAYMENT_CATALOG_SYNC_SECRET},json={
        'operator_name':'EVEVEV', 'stripe_account_id':ACCOUNT, 'location_id':STATION,
        'address':'Development fixture', 'postal_code':'00000','city':'Test','state':'TS','country':'USA',
        'station_id':STATION,'tenant_id':str(tenant),'ocpp_evse_id':1,'evse_id':STATION+'-1',
        'price_kwh':0.35,'authorization_amount':25,'currency':'usd'},timeout=30)
    catalog.raise_for_status()
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO payment_checkouts(connector_id,tariff_id,authorization_amount)
            SELECT pc.id,pc.tariff_id,2500 FROM payment_connectors pc JOIN payment_evses e ON e.id=pc.evse_id
            WHERE e.evse_id=%s RETURNING id''',(STATION+'-1',))
        cid=cur.fetchone()[0]
    conn.commit()
    intent=stripe.PaymentIntent.create(amount=2500,currency='usd',payment_method='pm_card_visa',
        payment_method_types=['card'],capture_method='manual',confirm=True,stripe_account=ACCOUNT,
        metadata={'checkoutId':str(cid),'purpose':'payment-recovery-development'})
    assert intent.status=='requires_capture'
    signed_webhook(cid,intent.id)
    print(json.dumps({'checkout_id':cid,'intent_id':intent.id,'mode':mode,
        'url':f'http://127.0.0.1:9010/charging/{STATION}-1/{cid}'}),flush=True)


if sys.argv[1]=='create':
    create(sys.argv[2])
elif sys.argv[1]=='inspect':
    cid=int(sys.argv[2])
    response=requests.get(API+f'/checkouts/{cid}',timeout=15)
    response.raise_for_status()
    data=response.json()
    intent=stripe.PaymentIntent.retrieve(data['payment_intent_id'],stripe_account=ACCOUNT)
    print(json.dumps({'checkout':data,'stripe_status':intent.status,'amount_capturable':intent.amount_capturable,'amount_received':intent.amount_received}))
elif sys.argv[1]=='stop':
    response=requests.post(API+f'/checkouts/{int(sys.argv[2])}/stop',timeout=30)
    print(response.status_code,response.text)
conn.close()
