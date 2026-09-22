import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useIntl } from 'react-intl';
import { Button, Dialog, ProgressBar, Toast } from 'antd-mobile';
import moment from 'moment';

import axios from '../util/Api.js';

export default function Charging() {
  const [state, setState] = React.useState({
    status: 'waiting', // (inital: 'waiting' / 'rejected' / 'charging' / 'closed' / 'error' )
    statusMessage: null,
    timestamp: '-',
    pricing: null,
    currency: '-',
    chargingTime: 0,
    transaction_kwh: 0,
    power_active_import: null,
    transaction_soc: null,
    evseStatus: null,
    initializing: true,
  });

  const [stopping, setStopping] = React.useState(false);

  const navigate = useNavigate();
  const intl = useIntl();
  const { evseId, sessionId } = useParams();

  const refreshTimer = React.useRef(null);
  // Live event stream (SSE). While it is connected the server pushes a fresh
  // checkout snapshot on every charger event, so no poll timers are scheduled.
  const eventSourceRef = React.useRef(null);

  // Shared mapping from a checkout payload (poll response or SSE frame) to
  // page state. Scheduling stays with the callers.
  const handleCheckoutData = React.useCallback((data) => {
    if (!data.id) {
      return;
    }
    let status = 'waiting';
    if (data.canceled_at) {
      status = 'canceled';
    } else if (
      data.cancellation_requested_at ||
      data.remote_request_status === 'Rejected'
    ) {
      status = 'canceling';
    } else if (data.transaction_end_time) {
      status = data.captured_at ? 'closed' : 'settling';
    } else if (data.captured_at) {
      status = 'closed';
    } else if (data.transaction_start_time) {
      status = 'charging';
    }
    setState((previous) => ({
      ...previous,
      ...data,
      status,
      evseStatus: data.evse_status ?? previous.evseStatus,
      timestamp: moment().format('DD-MM-YYYY HH:mm:ss'),
      chargingTime: data.transaction_start_time
        ? moment
            .utc(data.transaction_end_time || undefined)
            .diff(moment.utc(data.transaction_start_time), 'seconds')
        : 0,
    }));
  }, []);

  // Polling fallback (and manual refresh): fetch once; only chain a timer
  // when no event stream is connected.
  const setSessionData = React.useCallback(() => {
    axios
      .get(`checkouts/${sessionId}`)
      .then(({ data }) => {
        handleCheckoutData(data);
        if (eventSourceRef.current) {
          return;
        } // stream drives updates
        if (data.captured_at) {
          return;
        }
        if (data.id && data.transaction_start_time) {
          if (!data.captured_at) {
            refreshTimer.current = setTimeout(setSessionData, 30 * 1000); // 30s repeat timer, but only if we don't have an end_datetime yet
          }
        } else if (data.id && !data.transaction_start_time) {
          refreshTimer.current = setTimeout(setSessionData, 5000);
        }
      })
      .catch((e) => {
        // Set error on error
        setState((prevState) => ({
          ...prevState,
          status: 'error',
          statusMessage: e.response?.data?.detail,
        }));
      });
  }, [sessionId, handleCheckoutData]);

  React.useEffect(() => {
    if (!evseId) {
      // Navigate to home if no location data is given
      navigate('/');
      return undefined;
    }

    let es = null;
    if (typeof window.EventSource === 'function') {
      const base = (axios.defaults.baseURL || '').replace(/\/$/, '');
      es = new EventSource(`${base}/checkouts/${sessionId}/events`);
      eventSourceRef.current = es;
      es.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        handleCheckoutData(data);
        if (data.captured_at) {
          // Terminal state: the server ends the stream after the final
          // snapshot; close so EventSource doesn't auto-reconnect forever.
          es.close();
        }
      };
      es.onerror = () => {
        // EventSource retries transient drops (phone lock, network blip) by
        // itself. Only a permanently closed stream (e.g. HTTP error) is
        // terminal: fall back to the poll loop.
        if (es.readyState === EventSource.CLOSED && eventSourceRef.current) {
          eventSourceRef.current = null;
          setSessionData();
        }
      };
    } else {
      setSessionData();
    }

    // Cleanup: clear the timer and stream on unmount or re-render
    return () => {
      clearTimeout(refreshTimer.current);
      if (es) {
        es.close();
      }
      eventSourceRef.current = null;
    };
  }, [evseId, sessionId, navigate, handleCheckoutData, setSessionData]);

  const onRefresh = () => {
    // On manual refresh clear any pending timer and fetch immediately (with a
    // live stream connected this is a one-shot fetch, no new timer).
    clearTimeout(refreshTimer.current);
    setSessionData();
  };

  const onStopCharging = async () => {
    const confirmed = await Dialog.confirm({
      content: intl.formatMessage({
        id:
          state.status === 'waiting'
            ? 'charging.cancel.confirm'
            : 'charging.stop.confirm',
      }),
      confirmText: intl.formatMessage({
        id:
          state.status === 'waiting'
            ? 'charging.button.cancel'
            : 'charging.stop.confirm.yes',
      }),
      cancelText: intl.formatMessage({ id: 'charging.stop.confirm.no' }),
    });
    if (!confirmed) {
      return;
    }
    setStopping(true);
    try {
      const result = await axios.post(`checkouts/${sessionId}/stop`);
      if (
        result.data.status === 'Canceled' ||
        result.data.status === 'Canceling'
      ) {
        setState((previous) => ({
          ...previous,
          status: result.data.status === 'Canceled' ? 'canceled' : 'canceling',
        }));
      }
      setSessionData();
      Toast.show({
        content: intl.formatMessage({
          id:
            state.status === 'waiting'
              ? 'charging.cancel.requested'
              : 'charging.stop.requested',
        }),
      });
      // With a live stream the end event is pushed; in polling mode, poll
      // sooner than the 30s cadence so the page flips to 'closed' as soon
      // as the charger reports the transaction end.
      if (!eventSourceRef.current) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = setTimeout(setSessionData, 5000);
      }
      // `stopping` stays true: the button remains disabled until the status
      // leaves 'charging' and the button unmounts with it.
    } catch {
      setStopping(false);
      Toast.show({
        icon: 'fail',
        content: intl.formatMessage({ id: 'charging.stop.failed' }),
      });
    }
  };

  const getFormattedChargingTime = (seconds) => {
    let secs = seconds;
    const hours = secs / 3600;
    secs = secs % 3600;
    const mins = secs / 60;
    secs = secs % 60;
    return `${parseInt(hours, 10)}:${parseInt(mins, 10) < 10 ? `0${parseInt(mins, 10)}` : parseInt(mins, 10)}:${secs < 10 ? `0${parseInt(secs, 10)}` : parseInt(secs, 10)}`;
  };

  return (
    <div className="page-container" style={{ height: '100%', padding: '15px' }}>
      {/* Charging Speed */}
      <div className="charge-status">
        <div
          className={
            'charge-status__ring' +
            (state.status === 'rejected' || state.status === 'error'
              ? ' is-error'
              : '')
          }
        >
          {/* Icon depending on state */}
          {state.status === 'waiting' ? (
            <i className="ri-lock-unlock-fill"></i>
          ) : state.status === 'charging' ? (
            <i className="ri-flashlight-fill"></i>
          ) : state.status === 'rejected' ? (
            <i className="ri-error-warning-fill"></i>
          ) : state.status === 'closed' || state.status === 'canceled' ? (
            <i className="ri-check-double-fill"></i>
          ) : (
            <i className="ri-error-warning-fill"></i>
          )}
        </div>

        <div className="charge-status__headline">
          {/* Caption depending on state */}
          {state.status === 'canceled'
            ? intl.formatMessage({
                id:
                  state.cancellation_reason === 'inactivity'
                    ? 'charging.canceled.inactivity'
                    : 'charging.canceled',
              })
            : state.status === 'canceling'
              ? intl.formatMessage({ id: 'charging.cancel.requested' })
              : state.status === 'settling'
                ? intl.formatMessage({ id: 'charging.settling' })
                : state.status === 'waiting'
                  ? intl.formatMessage({
                      // Cable already in (EVSE 'Occupied' = 1.6 "Preparing"): the
                      // driver has done their part, so don't keep saying "plug in".
                      id:
                        state.evseStatus === 'Occupied'
                          ? 'charging.preparing'
                          : 'charging.authorized.waiting',
                    })
                  : state.status === 'charging'
                    ? !state.power_active_import && !state.transaction_kwh
                      ? // Session started but no energy flowing yet — the charger is
                        // still negotiating with the car, not actually charging.
                        intl.formatMessage({ id: 'charging.preparing' })
                      : `${intl.formatMessage({ id: 'charging.speed' })} ${state.power_active_import !== null ? `: ${Number(state.power_active_import).toFixed(2)} kW` : ''} `
                    : state.status === 'rejected'
                      ? intl.formatMessage({ id: 'charging.rejected' })
                      : state.status === 'closed'
                        ? intl.formatMessage({ id: 'charging.finished' })
                        : state.statusMessage
                          ? intl.formatMessage({ id: state.statusMessage })
                          : intl.formatMessage({ id: 'global.error.generic' })}
        </div>
        {/* Charging Speed END */}

        {/* Last update timestamp */}
        <div className="charge-status__sub">
          <i className="ri-refresh-fill"></i>{' '}
          {intl.formatMessage({ id: 'charging.lastupdate' })}: {state.timestamp}
        </div>

        {
          /* Refresh button only shown when waiting or charging */
          (state.status === 'charging' || state.status === 'waiting') && (
            <Button onClick={onRefresh} style={{ marginTop: '20px' }}>
              <i className="ri-refresh-line"></i>{' '}
              {intl.formatMessage({ id: 'charging.refresh' })}
            </Button>
          )
        }

        {
          /* Cancel is also available while the charger is preparing. */
          (state.status === 'charging' || state.status === 'waiting') && (
            <Button
              color="danger"
              block
              loading={stopping}
              disabled={stopping}
              onClick={onStopCharging}
              style={{ marginTop: '16px', minHeight: '48px' }}
            >
              <i className="ri-stop-circle-line"></i>{' '}
              {intl.formatMessage({
                id: stopping
                  ? 'charging.stop.requested'
                  : state.status === 'waiting'
                    ? 'charging.button.cancel'
                    : 'charging.button.stop',
              })}
            </Button>
          )
        }
      </div>

      {state.status === 'waiting' && (
        <p className="text-align-center" role="status">
          {intl.formatMessage({ id: 'charging.inactivity.notice' })}
        </p>
      )}
      {(state.status === 'canceled' || state.status === 'canceling') && (
        <div className="text-align-center" role="status">
          <p>
            {intl.formatMessage({
              id:
                state.status === 'canceled'
                  ? 'charging.hold.released'
                  : 'charging.hold.releasing',
            })}
          </p>
          {state.status === 'canceled' && (
            <Button
              block
              color="primary"
              onClick={() => navigate(`/checkout/${evseId}`)}
            >
              {intl.formatMessage({ id: 'charging.button.again' })}
            </Button>
          )}
        </div>
      )}
      {state.status !== 'rejected' &&
      state.status !== 'canceled' &&
      state.status !== 'canceling' ? (
        <>
          {/* Charging Costs: total_due = session costs + tax + transaction
              fee, the exact amount that will be captured */}
          <div className="width-100 div-with-margin charging-info-block">
            <div>
              {intl.formatMessage({ id: 'charging.costs' })} (
              {intl.formatMessage({ id: 'checkout.inclvat' })}):
            </div>
            <div>
              <span>
                {(
                  (state.pricing?.total_due ??
                    state.pricing?.total_costs_gross ??
                    0) / 100
                ).toFixed(2)}{' '}
              </span>
              <span>{state.pricing?.currency}</span>
            </div>
          </div>

          {/* Transaction fee, already included in the total above */}
          {state.pricing?.payment_costs_gross > 0 && (
            <div className="width-100 div-with-margin charging-info-block">
              <div>
                {intl.formatMessage({ id: 'charging.transactionfee' })} (
                {state.pricing?.payment_fee}%)
              </div>
              <div>
                <span>
                  {((state.pricing?.payment_costs_gross ?? 0) / 100).toFixed(2)}{' '}
                </span>
                <span>{state.pricing?.currency}</span>
              </div>
            </div>
          )}

          {/* Charging Time */}
          <div className="width-100 div-with-margin charging-info-block">
            <div>{intl.formatMessage({ id: 'charging.time' })}</div>
            <div>{getFormattedChargingTime(state.chargingTime)}</div>
          </div>

          {/* Energy delivered */}
          <div className="width-100 div-with-margin charging-info-block">
            <div>{intl.formatMessage({ id: 'charging.energy' })}</div>
            <div>{(state.transaction_kwh || 0).toFixed(2)} kWh</div>
          </div>

          {/* SoC */}
          {state.transaction_soc !== null && ( // only shown if we have an SoC
            <>
              <div className="width-100 div-with-margin charging-info-block">
                <div>{intl.formatMessage({ id: 'charging.soc' })}</div>
                {/* toFixed(2) kills float noise; Number() drops trailing zeros (27, 27.5) */}
                <div>{Number(Number(state.transaction_soc).toFixed(2))} %</div>
              </div>

              {/* SoC ProgressBar */}
              <ProgressBar
                percent={state.transaction_soc}
                style={{
                  width: '100%',
                  '--track-width': '35px',
                  '--fill-color': '#2d6a4f',
                }}
              />
              <span style={{ fontSize: '16px' }}>
                {intl.formatMessage({ id: 'charging.soc.infotext' })}
              </span>
            </>
          )}
        </>
      ) : state.status === 'rejected' ? (
        /* State is 'rejected' */
        <div className="text-align-center">
          {/* Don't worry */}
          <div
            className="width-100 div-with-margin charging-info-block"
            style={{ justifyContent: 'center' }}
          >
            {intl.formatMessage({ id: 'charging.dontworry' })}
          </div>

          {/* Please try again */}
          <div
            className="width-100 div-with-margin charging-info-block"
            style={{ justifyContent: 'center' }}
          >
            {intl.formatMessage({ id: 'charging.tryagain' })}
          </div>

          <Button
            color="primary"
            onClick={() => navigate(`/checkout/${evseId}`)}
          >
            <i className="ri-flashlight-line"></i>{' '}
            {intl.formatMessage({ id: 'charging.button.again' })}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
