/**
 * Drives the real viewer in a real Chromium against a real relay and agent.
 * Verifies the UI renders, login works, a device appears, a session opens
 * through WebCrypto, and actual pixels land on the canvas.
 *
 * usage: node tests/browser_check.mjs <baseUrl> <operator> <password> <devicePassword> <outDir>
 */
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const [base, user, pass, devPass, outDir] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  (${detail})` : ''}`);
};

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});

try {
  // ---------------- desktop ----------------
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#loginForm', { state: 'visible', timeout: 15000 });
  check('login page rendered', true, await page.title());
  check('Web Crypto available in this context',
    await page.evaluate(() => !!(window.crypto && window.crypto.subtle)));
  await page.screenshot({ path: `${outDir}/01-login.png` });

  await page.fill('#lu', user);
  await page.fill('#lp', pass);
  await page.click('#loginBtn');

  await page.waitForSelector('#fleetWrap:not(.hide)', { timeout: 15000 });
  await page.waitForSelector('.dev', { timeout: 25000 });
  const devName = await page.textContent('.dev .name');
  const devId = await page.textContent('.dev .id');
  check('fleet list shows the enrolled device', !!devName, `${devName} / ${devId}`);

  const chip = await page.textContent('#wsChip');
  check('viewer websocket authenticated in the browser', /admin/.test(chip), chip);

  const bars = await page.$$('.dev .bar');
  check('live CPU/RAM/disk metrics rendered', bars.length >= 2, `${bars.length} meters`);
  await page.screenshot({ path: `${outDir}/02-fleet.png` });

  // ---------------- session ----------------
  await page.click('.dev button.primary');
  await page.waitForSelector('#session:not(.hide)', { timeout: 10000 });
  await page.waitForSelector('#pwForm:not(.hide)', { timeout: 20000 });
  check('agent demanded the access password', true, 'password form shown');
  await page.screenshot({ path: `${outDir}/03-password.png` });

  // wrong password first
  await page.fill('#pwIn', 'wrong-password-here');
  await page.click('#pwForm button');
  await page.waitForSelector('#pwErr:not(.hide)', { timeout: 20000 });
  check('wrong password rejected in the UI', true, (await page.textContent('#pwErr')).trim());

  await page.fill('#pwIn', devPass);
  await page.click('#pwForm button');
  await page.waitForFunction(
    () => document.getElementById('overlay').classList.contains('hide'), { timeout: 25000 });
  check('correct password unlocked the session', true);

  // wait for real pixels
  await page.waitForFunction(() => {
    const c = document.getElementById('screen');
    return c && c.width > 100 && c.height > 100;
  }, { timeout: 20000 });

  const canvas = await page.evaluate(() => {
    const c = document.getElementById('screen');
    const ctx = c.getContext('2d');
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let nonBlack = 0, distinct = new Set();
    for (let i = 0; i < d.length; i += 4 * 997) {
      if (d[i] || d[i + 1] || d[i + 2]) nonBlack++;
      distinct.add(`${d[i] >> 4},${d[i + 1] >> 4},${d[i + 2] >> 4}`);
    }
    return { w: c.width, h: c.height, nonBlack, distinct: distinct.size };
  });
  check('remote screen decoded onto the canvas',
    canvas.nonBlack > 50 && canvas.distinct > 8,
    `${canvas.w}x${canvas.h}, ${canvas.nonBlack} sampled non-black px, ${canvas.distinct} distinct colours`);

  await page.click('#statsBtn');
  await page.waitForTimeout(2200);
  const stats = (await page.textContent('#stats')).replace(/\n/g, ' | ');
  check('stats overlay reports a live frame rate', /\d+(\.\d+)? fps/.test(stats), stats);
  await page.screenshot({ path: `${outDir}/04-session.png` });

  // ---------------- panels ----------------
  await page.click('#infoBtn');
  await page.waitForSelector('#panel:not(.hide)', { timeout: 8000 });
  await page.waitForFunction(() => {
    const b = document.getElementById('infoBody');
    return b && b.textContent && !b.textContent.includes('loading');
  }, { timeout: 15000 });
  const info = await page.textContent('#infoBody');
  check('system info panel populated', /Hostname|Ubuntu|Windows|macOS/.test(info), info.slice(0, 70).replace(/\s+/g, ' '));
  await page.screenshot({ path: `${outDir}/05-sysinfo.png` });

  await page.click('#filesBtn');
  await page.waitForSelector('#fList table', { timeout: 15000 });
  const rows = await page.$$('#fList tbody tr');
  check('remote file browser listed a directory', rows.length > 0, `${rows.length} entries`);
  await page.screenshot({ path: `${outDir}/06-files.png` });

  await page.click('#shellBtn');
  await page.waitForSelector('#term', { timeout: 8000 });
  await page.fill('#termIn', 'echo browser-shell-works && uname -sr');
  await page.press('#termIn', 'Enter');
  await page.waitForFunction(
    () => (document.getElementById('term')?.textContent || '').includes('browser-shell-works'),
    { timeout: 20000 });
  check('remote shell round-tripped from the browser', true,
    (await page.textContent('#term')).split('\n').filter((l) => l.includes('Linux') || l.includes('browser-shell-works')).slice(-1)[0]?.trim().slice(0, 50));
  await page.screenshot({ path: `${outDir}/07-shell.png` });

  await page.click('#panelClose');

  // ---------------- input ----------------
  await page.click('#screen', { position: { x: 200, y: 150 } });
  await page.keyboard.type('order 42');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(500);
  check('mouse and keyboard events sent without a page error', errors.length === 0,
    errors.length ? errors[0].slice(0, 90) : 'no console errors');

  // ---------------- admin ----------------
  await page.click('#hangupBtn');
  await page.waitForSelector('#fleetWrap:not(.hide)', { timeout: 10000 });
  await page.click('#adminBtn');
  await page.waitForFunction(
    () => (document.getElementById('adminBody')?.textContent || '').includes('Enroll'),
    { timeout: 15000 });
  const admin = await page.textContent('#adminBody');
  check('admin console rendered', /Enroll a new computer/.test(admin) && /Audit log/.test(admin));
  check('audit log has entries', (await page.$$('#adminBody table tbody tr')).length > 0,
    `${(await page.$$('#adminBody table tbody tr')).length} rows across tables`);
  await page.screenshot({ path: `${outDir}/08-admin.png`, fullPage: true });

  await ctx.close();

  // ---------------- phone ----------------
  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 3,
    isMobile: true, hasTouch: true,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
  });
  const p2 = await phone.newPage();
  await p2.goto(base, { waitUntil: 'domcontentloaded' });
  await p2.waitForSelector('#loginForm', { state: 'visible' });
  await p2.screenshot({ path: `${outDir}/09-phone-login.png` });
  await p2.fill('#lu', user);
  await p2.fill('#lp', pass);
  await p2.click('#loginBtn');
  await p2.waitForSelector('.dev', { timeout: 20000 });
  const cardBox = await (await p2.$('.dev')).boundingBox();
  check('phone layout fits the viewport without horizontal scroll',
    cardBox.width <= 390, `device card is ${Math.round(cardBox.width)}px wide`);
  await p2.screenshot({ path: `${outDir}/10-phone-fleet.png` });

  await p2.click('.dev button.primary');
  await p2.waitForSelector('#pwForm:not(.hide)', { timeout: 20000 });
  await p2.fill('#pwIn', devPass);
  await p2.click('#pwForm button');
  await p2.waitForFunction(
    () => document.getElementById('overlay').classList.contains('hide'), { timeout: 25000 });
  await p2.waitForFunction(() => {
    const c = document.getElementById('screen');
    return c && c.width > 100;
  }, { timeout: 20000 });
  await p2.click('#kbBtn');
  await p2.waitForSelector('#softkeys:not(.hide)');
  const keys = await p2.$$('#softkeys button');
  check('on-screen modifier keys available on mobile', keys.length >= 10, `${keys.length} softkeys`);
  await p2.waitForTimeout(1200);
  await p2.screenshot({ path: `${outDir}/11-phone-session.png` });
  await phone.close();
} catch (e) {
  check(`browser run threw: ${e.message.split('\n')[0]}`, false);
} finally {
  await browser.close();
}

const pass_ = results.filter((r) => r.ok).length;
console.log(`\n${'='.repeat(60)}\n  ${pass_}/${results.length} browser checks passed`);
for (const f of results.filter((r) => !r.ok)) console.log(`    FAILED: ${f.name} ${f.detail}`);
console.log(`  screenshots in ${outDir}\n${'='.repeat(60)}`);
process.exit(results.some((r) => !r.ok) ? 1 : 0);
