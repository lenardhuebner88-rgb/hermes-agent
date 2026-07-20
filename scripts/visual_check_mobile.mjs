#!/usr/bin/env node
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { requirePlaywrightChromium, resolveChromiumExecutable } from "./lib/playwright_chromium.mjs";
import { partitionConsoleErrors } from "./lib/visual_gate_http.mjs";

const chromium = requirePlaywrightChromium();

const CONTROL_URL = process.env.HERMES_VISUAL_GATE_URL || "http://127.0.0.1:9119/control";
const CONNECT_TIMEOUT_MS = 5_000;
const screenshotPath = process.env.HERMES_VISUAL_GATE_SCREENSHOT
  || path.join(os.tmpdir(), `hermes-visual-gate-mobile-${process.pid}.png`);

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
  const httpFailures = [];
  const startedAt = Date.now();
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
      if (message.type() === "error") {
        consoleErrors.push({
          text: message.text(),
          location: message.location(),
          atMs: Date.now() - startedAt,
        });
      }
    });
    page.on("pageerror", (error) => {
      consoleErrors.push({
        text: `pageerror: ${error.message}`,
        location: {},
        atMs: Date.now() - startedAt,
      });
    });
    page.on("response", (response) => {
      if (response.status() < 400) return;
      const request = response.request();
      httpFailures.push({
        status: response.status(),
        method: request.method(),
        url: response.url(),
        resourceType: request.resourceType(),
        isNavigationRequest: request.isNavigationRequest(),
        frameUrl: request.frame()?.url() ?? null,
        atMs: Date.now() - startedAt,
      });
    });

    await page.goto(CONTROL_URL, {
      waitUntil: "domcontentloaded",
      timeout: CONNECT_TIMEOUT_MS,
    });
    // Readiness anchor: the shared masthead (present on every viewport) proves
    // the SPA mounted. Do NOT wait on brand copy like "Hermes Control" — the
    // W2-a responsive shell (2026-07-10) dropped that visible text in favour of
    // the "Hermes:9119" health badge, leaving it only in document.title, which
    // getByText never matches → 15s timeout on an otherwise-healthy page.
    await page.getByTestId("control-masthead").filter({ visible: true }).first()
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

    const classified = partitionConsoleErrors({ consoleErrors, httpFailures, gateUrl: CONTROL_URL });
    if (classified.blocking.length > 0) {
      throw new Error(`console errors: ${classified.blocking.map((entry) => entry.text).join(" | ")}`);
    }

    process.stdout.write(JSON.stringify({
      ok: true,
      consoleErrors: [],
      httpFailures,
      toleratedConsoleErrors: classified.tolerated,
      overflowAfterFocus,
      focusTargetFound,
      screenshotPath,
    }) + "\n");
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const classified = partitionConsoleErrors({ consoleErrors, httpFailures, gateUrl: CONTROL_URL });
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
      consoleErrors: classified.blocking.map((entry) => entry.text),
      httpFailures,
      toleratedConsoleErrors: classified.tolerated,
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
