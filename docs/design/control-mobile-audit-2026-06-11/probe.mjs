// Mobile-Interaktions-Probe für das Hermes-Control-Dashboard (:9119).
// Fährt ein Pixel-5-artiges Viewport, klickt die hakeligen Stellen an und
// schreibt Screenshots + Befunde nach /tmp/hc-probe-*.
import { chromium, devices } from 'playwright';

const BASE = 'http://127.0.0.1:9119';
const findings = [];
const note = (s) => { findings.push(s); console.log('NOTE:', s); };

const browser = await chromium.launch();
const ctx = await browser.newContext({ ...devices['Pixel 5'] });
const page = await ctx.newPage();
page.setDefaultTimeout(8000);

async function shot(name) {
  await page.screenshot({ path: `/tmp/hc-probe-${name}.png` });
  console.log('SHOT', name);
}

// ── 1) Flow: Epic-Sheet öffnen → liegt der Submit-Button unter der Bottom-Nav?
await page.goto(`${BASE}/control/flow`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
await shot('flow-base');

// "Epic anlegen"-Button (auf Mobile nur Icon, aria-less? -> über Klasse/Icon suchen)
const epicBtn = page.locator('button:has(svg.lucide-layers):visible').first();
if (await epicBtn.count()) {
  await epicBtn.click();
  await page.waitForTimeout(400);
  await shot('flow-epic-sheet');
  const submit = page.locator('[role="dialog"] button', { hasText: 'Anlegen' }).first();
  if (await submit.count()) {
    const box = await submit.boundingBox();
    const vp = page.viewportSize();
    note(`Epic-Sheet Submit boundingBox y=${box?.y?.toFixed(0)} h=${box?.height?.toFixed(0)} viewport=${vp.height}`);
    // Ist an der Button-Position ein anderes Element obendrauf?
    if (box) {
      const topEl = await page.evaluate(([x, y]) => {
        const el = document.elementFromPoint(x, y);
        return el ? `${el.tagName}.${(el.className || '').toString().slice(0, 80)}` : 'none';
      }, [box.x + box.width / 2, box.y + box.height / 2]);
      note(`elementFromPoint über Epic-Submit: ${topEl}`);
    }
  } else { note('Epic-Sheet: Submit-Button nicht gefunden'); }
  await page.keyboard.press('Escape');
} else { note('Epic-anlegen-Button nicht gefunden'); }

// ── 2) Flow: Aufgabe-erfassen-Sheet (FlowCapture) — Höhe vs. Viewport
const capBtn = page.locator('button[aria-label="Neue Aufgabe erfassen"]:visible, button:has(svg.lucide-plus):visible').first();
if (await capBtn.count()) {
  // Der Glocken-FAB überlappt den Capture-FAB (eigener Fund) — force-click,
  // um trotzdem das Sheet selbst zu vermessen.
  await capBtn.click({ force: true, position: { x: 8, y: 40 } });
  await page.waitForTimeout(400);
  await shot('flow-capture-sheet');
  const dlg = page.locator('[role="dialog"]').last();
  const dbox = await dlg.boundingBox();
  const vp = page.viewportSize();
  if (dbox) note(`Capture-Sheet: y=${dbox.y.toFixed(0)} h=${dbox.height.toFixed(0)} (viewport ${vp.height}) — unten abgeschnitten: ${dbox.y + dbox.height > vp.height}`);
  const sub = page.locator('[role="dialog"] button', { hasText: 'Erfassen' }).first();
  if (await sub.count()) {
    const sbox = await sub.boundingBox();
    if (sbox) {
      const topEl = await page.evaluate(([x, y]) => {
        const el = document.elementFromPoint(x, y);
        return el ? `${el.tagName}.${(el.className || '').toString().slice(0, 80)}` : 'offscreen/none';
      }, [sbox.x + sbox.width / 2, sbox.y + sbox.height / 2]);
      note(`Capture-Submit y=${sbox.y.toFixed(0)} elementFromPoint: ${topEl}`);
    } else note('Capture-Submit: kein boundingBox (offscreen?)');
  }
  await page.keyboard.press('Escape');
}

// ── 3) Mehr-Menü (MoreNav, <details>-Pattern?) — öffnen, Outside-Tap, Navigation
await page.goto(`${BASE}/control`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
const mehr = page.locator('summary, button', { hasText: 'Mehr' }).first();
if (await mehr.count()) {
  const tag = await mehr.evaluate((el) => el.tagName);
  note(`Mehr-Trigger ist <${tag.toLowerCase()}>`);
  await mehr.click();
  await page.waitForTimeout(300);
  await shot('mehr-open');
  // Outside-Tap: schließt es?
  await page.touchscreen.tap(20, 500);
  await page.waitForTimeout(300);
  const stillOpen = await page.evaluate(() => !!document.querySelector('details[open]'));
  note(`Mehr-Menü nach Outside-Tap noch offen: ${stillOpen}`);
  await shot('mehr-after-outside-tap');
  // Falls offen: Eintrag klicken → schließt es nach Navigation?
  if (stillOpen) {
    const item = page.locator('details[open] a, details[open] button').first();
    if (await item.count()) {
      await item.click();
      await page.waitForTimeout(800);
      const openAfterNav = await page.evaluate(() => !!document.querySelector('details[open]'));
      note(`Mehr-Menü nach Navigation noch offen: ${openAfterNav}`);
      await shot('mehr-after-nav');
    }
  }
} else note('Mehr-Trigger nicht gefunden');

// ── 4) Body-Scroll-Lock-Check: Sheet offen → scrollt der Hintergrund?
await page.goto(`${BASE}/control/flow`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
if (await epicBtn.count()) {
  await epicBtn.click();
  await page.waitForTimeout(300);
  const y0 = await page.evaluate(() => window.scrollY);
  await page.mouse.wheel(0, 600);
  await page.waitForTimeout(300);
  const y1 = await page.evaluate(() => window.scrollY);
  note(`Scroll-Lock: scrollY vor=${y0} nach Wheel=${y1} (Hintergrund scrollt mit: ${y1 !== y0})`);
  await page.keyboard.press('Escape');
}

// ── 5) Touch-Target-Sweep auf /control (Start): interaktive Elemente < 40px
await page.goto(`${BASE}/control`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
const small = await page.evaluate(() => {
  const out = [];
  for (const el of document.querySelectorAll('button, a, [role="button"], summary')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.height < 40 || r.width < 40) {
      out.push(`${(el.textContent || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 30)} ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  }
  return out.slice(0, 25);
});
note(`Kleine Touch-Targets auf Start (<40px): ${small.length ? small.join(' | ') : 'keine'}`);

// ── 6) Horizontaler Overflow-Check auf allen Haupt-Routen
for (const r of ['', '/flow', '/statistik', '/bibliothek', '/backlog', '/pulse']) {
  await page.goto(`${BASE}/control${r}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  const overflow = await page.evaluate(() => {
    const docW = document.documentElement.clientWidth;
    const bad = [];
    for (const el of document.querySelectorAll('body *')) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && (rect.right > docW + 2 || rect.left < -2) && getComputedStyle(el).position !== 'fixed') {
        bad.push(`${el.tagName}.${(el.className || '').toString().slice(0, 60)} right=${Math.round(rect.right)}`);
        if (bad.length >= 4) break;
      }
    }
    return { docW, scrollW: document.documentElement.scrollWidth, bad };
  });
  note(`Route ${r || '/'}: scrollWidth=${overflow.scrollW} clientWidth=${overflow.docW}${overflow.scrollW > overflow.docW + 2 ? ' ⚠ H-OVERFLOW' : ''}${overflow.bad.length ? ' | ' + overflow.bad.join(' ; ') : ''}`);
}

await browser.close();
console.log('\n==== FINDINGS ====');
for (const f of findings) console.log('-', f);
