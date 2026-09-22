// Local OCPP 1.6 fixture for payment recovery integration tests.
const WebSocket = require('../../ocpp-virtual-charge-point/node_modules/ws');
const http = require('node:http');
const { randomUUID } = require('node:crypto');
const station = process.env.RECOVERY_STATION || 'payment-recovery-20260921';
const ws = new WebSocket(`ws://127.0.0.1:8081/${station}`, ['ocpp1.6']);
const pending = new Map();
let mode = 'no-start', transaction = null, tag = null, meter = 100000, meterTimer;
function call(action, payload) {
  const id = randomUUID();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${action} timed out`)); }, 15000);
    pending.set(id, result => { clearTimeout(timer); resolve(result); });
    ws.send(JSON.stringify([2, id, action, payload]));
  });
}
const stamp = () => new Date().toISOString();
const status = value => call('StatusNotification', { connectorId: 1, errorCode: 'NoError', status: value, timestamp: stamp() });
async function stop() {
  clearInterval(meterTimer);
  if (transaction !== null) {
    const tx = transaction; transaction = null;
    await call('StopTransaction', { transactionId: tx, idTag: tag, meterStop: meter, reason: 'Remote', timestamp: stamp() });
  }
  await status('Available');
}
ws.on('message', async raw => {
  try {
    const [kind, id, action, body] = JSON.parse(raw);
    if (kind === 3 || kind === 4) { pending.get(id)?.(action); pending.delete(id); return; }
    let response = { status: 'Accepted' };
    if (action === 'GetConfiguration') response = { configurationKey: [{ key: 'SupportedFeatureProfiles', readonly: true, value: 'Core,RemoteTrigger' }] };
    if (action === 'RemoteStartTransaction' && mode === 'reject') response = { status: 'Rejected' };
    ws.send(JSON.stringify([3, id, response]));
    if (action === 'RemoteStartTransaction') {
      tag = body.idTag;
      console.log(JSON.stringify({ action, tag, mode, response }));
      if (mode === 'reject') return;
      await status('Preparing');
      if (mode === 'failed-start') { setTimeout(() => status('Available').catch(console.error), 1000); return; }
      if (mode === 'charging' || mode === 'zero-energy') {
        const reply = await call('StartTransaction', { connectorId: 1, idTag: tag, meterStart: meter, timestamp: stamp() });
        transaction = reply.transactionId;
        if (reply.idTagInfo?.status !== 'Accepted') { await stop(); return; }
        await status('Charging');
        meterTimer = setInterval(() => {
          if (mode === 'charging') meter += 100;
          call('MeterValues', { connectorId: 1, transactionId: transaction, meterValue: [{ timestamp: stamp(), sampledValue: [
            { value: String(meter), measurand: 'Energy.Active.Import.Register', unit: 'Wh' },
            { value: mode === 'charging' ? '7200' : '0', measurand: 'Power.Active.Import', unit: 'W' },
          ] }] }).catch(console.error);
        }, 2000);
      }
    } else if (action === 'RemoteStopTransaction') await stop();
  } catch (error) { console.error(error.message); }
});
ws.on('open', async () => {
  await call('BootNotification', { chargePointVendor: 'RecoveryTest', chargePointModel: 'AC_2_Charge', firmwareVersion: 'test', chargePointSerialNumber: station });
  await status('Available');
  console.log(JSON.stringify({ ready: true, station }));
});
ws.on('error', error => console.error(error.message));
const heartbeat = setInterval(() => { if (ws.readyState === WebSocket.OPEN) call('Heartbeat', {}).catch(console.error); }, 30000);
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (req.method === 'POST') {
    if (url.pathname === '/stop') await stop();
    else if (['no-start', 'reject', 'failed-start', 'charging', 'zero-energy'].includes(url.searchParams.get('mode'))) mode = url.searchParams.get('mode');
  }
  res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ station, mode, transaction, tag }));
});
server.listen(Number(process.env.RECOVERY_CONTROL_PORT || 9197), '127.0.0.1');
process.on('SIGTERM', () => { clearInterval(heartbeat); clearInterval(meterTimer); ws.close(); server.close(); setTimeout(() => process.exit(), 100); });
