// Browser regression against the real development API (no mocked responses).
const { chromium } = require('../../citrineos-core/apps/operator-ui/node_modules/@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const cwd = path.resolve(__dirname, '..');
const cid = process.argv[2];
const running = process.argv[3] === 'running';
if (!/^\d+$/.test(cid || '')) throw new Error('Pass a development checkout id');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || '/home/mingcan/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'});
  const page = await browser.newPage({ viewport:{width:390,height:844}, isMobile:true, deviceScaleFactor:1 });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => Object.defineProperty(window,'CLIENT_API_URL',{ get:()=> 'http://127.0.0.1:9010/api',set:()=>{} }));
  await page.goto(`http://127.0.0.1:9010/charging/payment-recovery-20260921-1/${cid}`);
  const button = page.getByRole('button',{name:running ? /Stop charging/ : /Cancel start/});
  await button.waitFor();
  const bounds = await button.boundingBox();
  if (!bounds || bounds.height < 44 || bounds.x + bounds.width > 391) throw new Error('Cancel button fails mobile sizing');
  await page.screenshot({path:'/tmp/payment-mobile-prestart.png',fullPage:true});
  await button.click();
  if (running) {
    await page.getByRole('button',{name:'Stop',exact:true}).click();
    await page.getByText(/Session finished|Session canceled/).waitFor({timeout:20000});
  } else {
    await page.getByText('Cancel this start and release the card authorization?',{exact:true}).waitFor();
    await page.getByRole('button',{name:/Cancel start/}).last().click();
    await page.getByText('Session canceled',{exact:true}).waitFor({timeout:20000});
    await page.getByText('Your card authorization has been canceled.',{exact:false}).waitFor();
  }
  await page.screenshot({path:'/tmp/payment-mobile-canceled.png',fullPage:true});
  const result = JSON.parse(execFileSync(path.join(cwd,'.venv/bin/python'),['scripts/payment_recovery_dev.py','inspect',cid],{cwd,encoding:'utf8'}));
  if(!(running ? ['succeeded','canceled'] : ['canceled']).includes(result.stripe_status) || result.amount_capturable !== 0) throw new Error('Stripe hold was not settled/released');
  if(errors.length) throw new Error(errors.join('\n'));
  console.log(JSON.stringify({checkout_id:cid,mobile_cancel:'passed',stripe_status:result.stripe_status,remaining_hold:result.amount_capturable,viewport:'390x844'}));
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
