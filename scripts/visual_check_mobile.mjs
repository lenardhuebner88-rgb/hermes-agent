#!/usr/bin/env node
import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("/home/piet/.hermes/hermes-agent/node_modules/playwright-core");

const CONTROL_URL = process.env.HERMES_VISUAL_GATE_URL || "http://127.0.0.1:9119/control";
const CONNECT_TIMEOUT_MS = 5_000;
const screenshotPath = process.env.HERMES_VISUAL_GATE_SCREENSHOT
  || path.join(os.tmpdir(), `hermes-visual-gate-mobile-${process.pid}.png`);

function resolveChromiumExecutable() {
  const root = path.join(os.homedir(), ".cache", "ms-playwright");
  const entries = fs.readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith("chromium-"))
    .map((entry) => entry.name)
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));

  for (const name of entries.reverse()) {
    for (const chromeDir of ["chrome-linux64", "chrome-linux"]) {
      const candidate = path.join(root, name, chromeDir, "chrome");
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  throw new Error(`Chromium binary not found under ${root}`);
}

async function clickFirstUsefulButton(page) {
  const preferred = page.getByRole("button", { name: /Flow|Statistik|Stats|Start/i })
    .filter({ visible: true })
    .first();
  if (await preferred.count()) {
    await preferred.click({ timeout: CONNECT_TIMEOUT_MS });
    return;
  }
  const firstButton = page.locator("button").filter({ visible: true }).first();
  if (!(await firstButton.count())) {
    throw new Error("No visible button found for mobile interaction");
  }
  await firstButton.click({ timeout: CONNECT_TIMEOUT_MS });
}

async function typeIntoVisibleInput(page) {
  const input = page.locator(
    "input:not([type='hidden']):not([disabled]), "
      + "textarea:not([disabled]), "
      + "[contenteditable='true'], "
      + "[role='textbox']",
  ).filter({ visible: true }).first();
  if (!(await input.count())) return false;
  await input.click({ timeout: CONNECT_TIMEOUT_MS });
  await input.type("gate", { delay: 10, timeout: CONNECT_TIMEOUT_MS });
  return true;
}

async function readOverflow() {
  return document.documentElement.scrollWidth <= window.innerWidth
    ? {
        ok: true,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }
    : {
        ok: false,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      };
}

async function main() {
  const consoleErrors = [];
  let overflowAfterFocus = null;
  let focusTargetFound = false;
  let browser = null;

  try {
    browser = await chromium.launch({
      executablePath: resolveChromiumExecutable(),
      headless: true,
      args: ["--no-sandbox"],
    });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
      userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
        + "Mobile/15E148 Safari/604.1",
    });
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => {
      consoleErrors.push(`pageerror: ${error.message}`);
    });

    await page.goto(CONTROL_URL, {
      waitUntil: "domcontentloaded",
      timeout: CONNECT_TIMEOUT_MS,
    });
    await page.getByText("Hermes Control").filter({ visible: true }).first()
      .waitFor({ timeout: 15_000 });

    const overflowBeforeInteraction = await page.evaluate(readOverflow);
    if (!overflowBeforeInteraction.ok) {
      throw new Error(
        `horizontal overflow before interaction: scrollWidth=${overflowBeforeInteraction.scrollWidth} `
        + `innerWidth=${overflowBeforeInteraction.innerWidth}`,
      );
    }

    await clickFirstUsefulButton(page);
    await page.waitForTimeout(250);
    focusTargetFound = await typeIntoVisibleInput(page);
    await page.waitForTimeout(250);

    overflowAfterFocus = await page.evaluate(readOverflow);
    if (!overflowAfterFocus.ok) {
      throw new Error(
        `horizontal overflow after focus: scrollWidth=${overflowAfterFocus.scrollWidth} `
        + `innerWidth=${overflowAfterFocus.innerWidth}`,
      );
    }

    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(150);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: screenshotPath, fullPage: true });

    if (consoleErrors.length > 0) {
      throw new Error(`console errors: ${consoleErrors.join(" | ")}`);
    }

    process.stdout.write(JSON.stringify({
      ok: true,
      consoleErrors,
      overflowAfterFocus,
      focusTargetFound,
      screenshotPath,
    }) + "\n");
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      if (browser) {
        const contexts = browser.contexts();
        const pages = contexts.flatMap((context) => context.pages());
        if (pages[0]) await pages[0].screenshot({ path: screenshotPath, fullPage: true });
      }
    } catch {
      // Best-effort failure artifact only.
    }
    process.stdout.write(JSON.stringify({
      ok: false,
      consoleErrors,
      overflowAfterFocus,
      focusTargetFound,
      screenshotPath,
      error: message,
    }) + "\n");
    return 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

process.exitCode = await main();
