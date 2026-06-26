import React from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useIntl } from 'react-intl';
import { Button, Checkbox } from 'antd-mobile';
import { Modal, Skeleton } from 'antd';

import PaymentOptions from '../components/PaymentOptions.js';

import axios from '../util/Api.js';
import getConnectorPowerKw from '../util/ConnectorCalculatePower.js';
import { getLocationData } from '../util/getLocationData.js';

const httpProtocol = process.env.REACT_APP_SSL_ENABLED ? 'https' : 'http';
const currencies = {
  EUR: '€',
  USD: '$',
};

export default function Checkout() {
  const [state, setState] = React.useState({
    power_type: null,
    max_voltage: null,
    max_amperage: null,
    address: null,
    postalCode: null,
    city: null,
    state: null,
    country: null,
    operator: null,
    payment_terms_conditions: null,
    tariff_data: null,
    errMsg: null,
    taChecked: false,
    loading: false,
    initializing: true,
    status: 'UNKNOWN',
    modalVisible: false,
  });
  const navigate = useNavigate();
  const intl = useIntl();
  const { evseId } = useParams();
  // Scan & Charge: the QR on the charger points here with the already-created
  // Stripe payment link in the `pay` param, so the driver sees charger/tariff
  // details and taps "Pay now" to go to Stripe (instead of the QR opening Stripe
  // directly). When absent, this is the normal web-portal flow that creates a
  // checkout session on demand.
  const [searchParams] = useSearchParams();
  const payNowUrl = searchParams.get('pay');

  React.useEffect(() => {
    const setLocationData = (location) => {
      setState((prevState) => ({
        ...prevState,
        ...location,
        initializing: false,
      }));
    };
    if (evseId) {
      axios
        .get(`evses/${evseId}`)
        .then(async ({ data }) => {
          // Check if location given, else forward to home
          if (data.id) {
            const location_data = await getLocationData(data);
            // const connector_data = data.connectors?.[0]
            // delete data.connectors;

            // const location_data = (await axios.get(`locations/${data.location_id}`)).data;
            // const operator = location_data.operator?.name;
            // delete location_data.operator;
            // // const operator_data = (await axios.get(`operators/${location_data.operator_id}`)).data;
            // const tariff_data = (await axios.get(`tariffs/${connector_data.tariff_id}`)).data;
            // delete tariff_data.id;

            setLocationData(location_data);
          } else {
            // Navigate to home with error
            navigate('/', {
              state: {
                evseId: evseId,
                errMsg: data.message ? data.message : 'global.error.generic',
              },
            });
          }
        })
        .catch(() => {
          // Navigate to home with error
          navigate('/', {
            state: { evseId: evseId, errMsg: 'global.error.generic' },
          });
        });
    } else {
      // Navigate to home if no location data is given
      navigate('/');
    }
  }, [evseId, navigate]);

  const onCheckout = () => {
    setState({ ...state, errMsg: null });

    // Check if TA accepted, if not show error
    if (!state.taChecked) {
      setState({
        ...state,
        errMsg: intl.formatMessage({ id: 'checkout.error.tanotaccepted' }),
      });
      return false;
    }

    // Scan & Charge: a payment link already exists for this (already-started)
    // session — send the driver straight to it instead of creating a new
    // checkout session (which would start a second transaction).
    if (payNowUrl) {
      setState({ ...state, loading: true });
      window.location.href = payNowUrl;
      return true;
    }

    // TA accpeted, process checkout
    setState({ ...state, loading: true });
    axios
      .post(`checkouts/`, {
        evse_id: evseId,
        success_url: `${httpProtocol}://${window.location.host}/charging/${evseId}`,
        cancel_url: `${httpProtocol}://${window.location.host}/checkout/${evseId}`,
      })
      .then(({ data }) => {
        // Check if checkout given,
        if (data?.url) {
          window.location.replace(data.url);
        } else {
          setState({
            ...state,
            loading: false,
            errMsg: intl.formatMessage({ id: 'global.error.generic' }),
          });
        }
      })
      .catch(() => {
        setState({
          ...state,
          loading: false,
          errMsg: intl.formatMessage({ id: 'global.error.generic' }),
        });
      });
  };

  const get_price = (net_price) => {
    const price = net_price * (1 + state.tariff_data?.tax_rate / 100);
    return price.toFixed(2);
  };

  // currency comes back lower-cased (e.g. "usd"); the symbol map is keyed
  // upper-case. Fall back to the raw code so we never show a bare number.
  const cur =
    currencies[state.tariff_data?.currency?.toUpperCase()] ||
    (state.tariff_data?.currency
      ? `${state.tariff_data.currency.toUpperCase()} `
      : '');

  return (
    <div className="page-container">
      <div className="charger-card">
        <Skeleton active loading={state.initializing}>
          <h1 className="charger-card__title">
            {state.operator || 'This charging point'}
          </h1>

          {state.address && (
            <div className="charger-place">
              <i className="ri-map-pin-line" />
              <span>
                {state.address}, {state.postalCode} {state.city}
                {state.state ? `, ${state.state}` : ''}
              </span>
            </div>
          )}

          <div className="charger-ref">
            {evseId} · max{' '}
            {getConnectorPowerKw(
              state.max_voltage,
              state.max_amperage,
              state.power_type,
            )}{' '}
            kW
            {state.status && state.status !== 'UNKNOWN' && (
              <>
                {' · '}
                <span className={state.status === 'Available' ? 'avail' : undefined}>
                  {state.status}
                </span>
              </>
            )}
          </div>

          <div className="card-divider" />

          <div className="rate">
            {state.tariff_data?.price_kwh > 0 && (
              <div className="rate__main">
                <span className="rate__amt">
                  {cur}
                  {get_price(state.tariff_data?.price_kwh)}
                </span>
                <span className="rate__unit">
                  / {intl.formatMessage({ id: 'checkout.pricekwh' })}
                </span>
                <span className="rate__vat">
                  · {intl.formatMessage({ id: 'checkout.inclvat' })}
                </span>
              </div>
            )}
            {state.tariff_data?.price_min > 0 && (
              <div className="rate__row">
                {cur}
                {get_price(state.tariff_data?.price_min)} /{' '}
                {intl.formatMessage({ id: 'checkout.pricemin' })}
              </div>
            )}
            {state.tariff_data?.price_session > 0 && (
              <div className="rate__row">
                {cur}
                {get_price(state.tariff_data?.price_session)} /{' '}
                {intl.formatMessage({ id: 'checkout.pricesession' })}
              </div>
            )}
          </div>

          <div className="auth-note">
            {intl.formatMessage(
              { id: 'checkout.authinfo' },
              {
                authorzation_amount: (
                  <b>
                    {cur}
                    {state.tariff_data?.authorization_amount}
                  </b>
                ),
              },
            )}
          </div>

          <div className="pay-options">
            <PaymentOptions />
          </div>

          <Button
            className="cta-button"
            color="primary"
            block
            onClick={onCheckout}
            loading={state.loading}
          >
            <i className="ri-flashlight-line" />{' '}
            {payNowUrl
              ? intl.formatMessage({ id: 'checkout.button.paynow' })
              : intl.formatMessage({ id: 'checkout.button.checkout' })}
          </Button>

          {state.errMsg && <div className="checkout-error">{state.errMsg}</div>}

          <div className="terms-line">
            <Checkbox
              value={state.taChecked}
              onChange={(val) => {
                setState({ ...state, taChecked: val, errMsg: null });
              }}
            >
              <span style={{ fontSize: '12.5px', color: 'var(--ink-soft)' }}>
                {intl.formatMessage({ id: 'checkout.accept.terms.prefix' })}{' '}
                <a
                  href="#"
                  onClick={() => {
                    setState({ ...state, modalVisible: true });
                  }}
                >
                  {intl.formatMessage({ id: 'checkout.accept.terms.linktext' })}
                </a>
              </span>
            </Checkbox>
          </div>

          <div className="secured-line">
            <i className="ri-lock-2-line" />
            {intl.formatMessage({ id: 'checkout.securedbystripe' })}
          </div>
        </Skeleton>
      </div>

      <Modal
        visible={state.modalVisible}
        footer={null}
        onCancel={() => setState({ ...state, modalVisible: false })}
      >
        <div
          dangerouslySetInnerHTML={{ __html: state.payment_terms_conditions }}
        ></div>
      </Modal>
    </div>
  );
}
