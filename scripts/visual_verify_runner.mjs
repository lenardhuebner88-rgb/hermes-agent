#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import { requirePlaywrightChromium, resolveChromiumExecutable } from "./lib/playwright_chromium.mjs";

const CONNECT_TIMEOUT_MS = 15_000;
const DEFAULT_VIEWPORTS = [
  { name: "mobile-390", width: 390, height: 844, isMobile: true, hasTouch: true },
  { name: "tablet-820", width: 820, height: 1180, isMobile: true, hasTouch: true },
  { name: "desktop-1366", width: 1366, height: 900, isMobile: false, hasTouch: false },
];

function usage() {
  process.stderr.write(
    "usage: visual_verify_runner.mjs --base-url URL --output-dir DIR --git-head SHA "
    + "[--viewports WxH[,name=WxH...]] [--scenario terminal_bridge] [--interactive] <route> [<route>...]\n",
  );
}

function parseViewportsSpec(spec) {
  const entries = spec.split(",").map((entry) => entry.trim());
  if (entries.length === 0 || entries.some((entry) => entry.length === 0)) {
    throw new Error(`invalid --viewports spec: "${spec}" (empty entry — expected e.g. "390x844,tablet-lg=840x1118")`);
  }
  const viewports = entries.map((entry) => {
    const eq = entry.indexOf("=");
    const name = eq === -1 ? "" : entry.slice(0, eq);
    const dims = eq === -1 ? entry : entry.slice(eq + 1);
    const match = /^(\d+)x(\d+)$/.exec(dims);
    if (!match || (eq !== -1 && name.length === 0)) {
      throw new Error(`invalid --viewports entry: "${entry}" (expected WxH or name=WxH)`);
    }
    if (eq !== -1 && !/^[A-Za-z0-9._-]+$/.test(name)) {
      throw new Error(`invalid --viewports entry: "${entry}" (name must be [A-Za-z0-9._-])`);
    }
    const width = Number(match[1]);
    const height = Number(match[2]);
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || width > 10000 || height < 1 || height > 10000) {
      throw new Error(`invalid --viewports entry: "${entry}" (width/height must be 1..10000)`);
    }
    const isMobile = width < 1024;
    return {
      name: name || `w${width}`,
      width,
      height,
      isMobile,
      hasTouch: isMobile,
    };
  });
  const seen = new Set();
  for (const viewport of viewports) {
    if (seen.has(viewport.name)) {
      throw new Error(`invalid --viewports spec: duplicate viewport name "${viewport.name}"`);
    }
    seen.add(viewport.name);
  }
  return viewports;
}

function parseArgs(argv) {
  let baseUrl = "";
  let outputDir = "";
  let gitHead = "";
  let viewportsSpec = null;
  let scenario = "default";
  let interactive = false;
  const routes = [];
  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--base-url") {
      baseUrl = argv[++index] || "";
    } else if (arg === "--output-dir") {
      outputDir = argv[++index] || "";
    } else if (arg === "--git-head") {
      gitHead = argv[++index] || "";
    } else if (arg === "--viewports") {
      viewportsSpec = argv[++index] || "";
    } else if (arg === "--scenario") {
      scenario = argv[++index] || "";
    } else if (arg === "--interactive") {
      interactive = true;
    } else if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(0);
    } else if (arg.startsWith("--")) {
      throw new Error(`unknown option: ${arg}`);
    } else {
      routes.push(arg);
    }
  }
  if (!baseUrl || !outputDir || !/^[0-9a-f]{40}$/.test(gitHead) || routes.length === 0
      || !["default", "terminal_bridge"].includes(scenario)) {
    usage();
    process.exit(2);
  }
  let viewports = DEFAULT_VIEWPORTS;
  if (viewportsSpec !== null) {
    try {
      viewports = parseViewportsSpec(viewportsSpec);
    } catch (error) {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      usage();
      process.exit(2);
    }
  }
  return { baseUrl, outputDir, gitHead, routes, viewports, scenario, interactive };
}

function routeUrl(baseUrl, route) {
  return new URL(route.startsWith("/") ? route : `/${route}`, baseUrl).toString();
}

function safeName(route, viewport) {
  const slug = route.replace(/^\/+/, "").replace(/[^a-zA-Z0-9._-]+/g, "-") || "root";
  return `${slug}-${viewport.name}.png`;
}

async function readOverflow() {
  return {
    ok: document.documentElement.scrollWidth <= window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
}

async function readUxSignals() {
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0 && rect.width > 0 && rect.height > 0;
  };
  const label = (element) => {
    const labelledBy = element.getAttribute("aria-labelledby");
    const labelledText = labelledBy
      ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "").join(" ")
      : "";
    return (
      element.getAttribute("aria-label")
      || labelledText
      || element.getAttribute("title")
      || element.textContent
      || element.getAttribute("value")
      || ""
    ).trim().replace(/\s+/g, " ").slice(0, 120);
  };
  const describe = (element) => {
    const id = element.id ? `#${element.id}` : "";
    const role = element.getAttribute("role");
    return `${element.tagName.toLowerCase()}${id}${role ? `[role=${role}]` : ""}`;
  };
  const controls = Array.from(document.querySelectorAll(
    "button, input, select, textarea, [role=button], [role=tab], [role=switch]",
  )).filter(visible);
  const undersizedControls = [];
  const unlabeledControls = [];
  for (const element of controls) {
    const rect = element.getBoundingClientRect();
    const item = {
      element: describe(element),
      label: label(element),
      width: Math.round(rect.width * 10) / 10,
      height: Math.round(rect.height * 10) / 10,
    };
    // Strenger, maschinenlesbarer Hinweis fuer WCAG 2.5.8. Der Verifier
    // beurteilt Ausnahmen und darf nachher nie mehr Verstösse akzeptieren.
    if (rect.width < 24 || rect.height < 24) undersizedControls.push(item);
    if (!item.label && element.getAttribute("aria-hidden") !== "true") unlabeledControls.push(item);
  }
  return {
    interactiveControlCount: controls.length,
    // Rendered-Text-Laenge als zweites, unabhaengiges Inhaltssignal: eine
    // Seite, die weder Bedienelemente noch nennenswerten Text hat, ist nicht
    // fertig geladen. Siehe emptyPage-Check unten.
    renderedTextLength: (document.body?.innerText ?? "").trim().length,
    undersizedControls: undersizedControls.slice(0, 50),
    unlabeledControls: unlabeledControls.slice(0, 50),
    truncated: undersizedControls.length > 50 || unlabeledControls.length > 50,
  };
}

// Eine leere Seite besteht sonst JEDEN Check: keine Konsolenfehler, kein
// Overflow, und `undersizedControls: []` / `unlabeledControls: []` sind auf
// einer Seite ohne Controls trivial wahr. Belegt am 2026-07-28: /control/loops
// lieferte auf mobile-390 ein leeres <main>, interactiveControlCount 0 — und
// summary.json meldete `status: "passed"`, `allPassed: true`. Das ist dieselbe
// Evidenz, auf die sich die Loop-Verifier stuetzen.
const EMPTY_PAGE_TEXT_FLOOR = 200;

function isEmptyPage(uxSignals) {
  if (!uxSignals) return false;
  return uxSignals.interactiveControlCount === 0
    && (uxSignals.renderedTextLength ?? 0) < EMPTY_PAGE_TEXT_FLOOR;
}

async function clickFirstVisible(locator) {
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false)) {
      await candidate.click();
      return true;
    }
  }
  return false;
}

async function readTerminalGeometry(viewport) {
  const visible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const terminals = Array.from(document.querySelectorAll('[data-testid^="terminal-pane-host-"]')).filter(visible);
  const terminal = terminals[0] || null;
  const terminalRect = terminal?.getBoundingClientRect() || null;
  const bottomNav = Array.from(document.querySelectorAll('nav[aria-label="Navigation"]')).find(visible) || null;
  const navRect = bottomNav?.getBoundingClientRect() || null;
  const minimumWidth = viewport.width < 500 ? 280 : viewport.width < 1000 ? 420 : 520;
  const clearance = terminalRect && navRect ? navRect.top - terminalRect.bottom : null;
  return {
    terminal_width_px: terminalRect ? Math.round(terminalRect.width * 10) / 10 : 0,
    terminal_width_min_px: minimumWidth,
    terminal_width_usable: Boolean(terminalRect && terminalRect.width >= minimumWidth),
    bottom_navigation_clearance_px: clearance === null ? null : Math.round(clearance * 10) / 10,
    bottom_navigation_clear: clearance === null || clearance >= 0,
  };
}

async function openTerminalBridgeFixture(page) {
  await page.locator('[data-testid^="terminal-pane-host-"]').first().waitFor({ state: "visible", timeout: CONNECT_TIMEOUT_MS });
  const handoff = page.getByRole("button", { name: /Handoff öffnen/ });
  if (!await clickFirstVisible(handoff)) {
    const tools = page.getByRole("button", { name: "Werkzeuge umschalten" });
    if (!await clickFirstVisible(tools)) {
      await clickFirstVisible(page.locator('button[aria-label*="codex" i], button[title*="codex" i]'));
    }
    if (!await clickFirstVisible(handoff)) {
      await clickFirstVisible(page.locator('button[aria-label*="codex" i], button[title*="codex" i]'));
      if (!await clickFirstVisible(handoff)) throw new Error("terminal_bridge Handoff button is not visible");
    }
  }
  const heldCandidate = page.getByText("Kandidaten gehalten einreichen", { exact: false });
  await heldCandidate.first().waitFor({ state: "visible", timeout: CONNECT_TIMEOUT_MS });
  return {
    handoff_visible: true,
    held_candidate_visible: true,
  };
}

async function checkOne(browser, baseUrl, outputDir, route, viewport, scenario) {
  const consoleErrors = [];
  const pageErrors = [];
  const screenshotPath = path.join(outputDir, safeName(route, viewport));
  const ariaSnapshotPath = screenshotPath.replace(/\.png$/, ".aria.yml");
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    isMobile: viewport.isMobile,
    hasTouch: viewport.hasTouch,
  });
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });

  let overflow = null;
  let uxSignals = null;
  let ariaSnapshotError = null;
  let checks = {};
  let ok = true;
  let error = null;
  try {
    await page.goto(routeUrl(baseUrl, route), { waitUntil: "domcontentloaded", timeout: CONNECT_TIMEOUT_MS });
    await page.waitForLoadState("networkidle", { timeout: CONNECT_TIMEOUT_MS }).catch(() => {});
    // Auf gerenderten Inhalt warten statt auf eine feste Frist: die Views sind
    // lazy-importierte Chunks, deren Render nach `networkidle` noch aussteht.
    // Ohne dieses Warten kam am 2026-07-28 reproduzierbar ein leerer erster
    // Screenshot zustande (kalter Chunk-Cache). Laeuft die Frist ab, faengt der
    // emptyPage-Check unten den Fall — hier wird nichts verschluckt.
    await page.waitForFunction(
      (floor) => {
        const body = document.body;
        if (!body) return false;
        const controls = body.querySelectorAll(
          "button, input, select, textarea, [role=button], [role=tab], [role=switch]",
        ).length;
        return controls > 0 || (body.innerText ?? "").trim().length >= floor;
      },
      EMPTY_PAGE_TEXT_FLOOR,
      { timeout: CONNECT_TIMEOUT_MS },
    ).catch(() => {});
    await page.waitForTimeout(250);
    overflow = await page.evaluate(readOverflow);
    uxSignals = await page.evaluate(readUxSignals);
    const geometry = scenario === "terminal_bridge"
      ? await page.evaluate(readTerminalGeometry, viewport)
      : {};
    const fixture = scenario === "terminal_bridge"
      ? await openTerminalBridgeFixture(page)
      : {};
    try {
      const ariaSnapshot = await page.locator("body").ariaSnapshot();
      await fs.writeFile(ariaSnapshotPath, `${ariaSnapshot.trimEnd()}\n`, "utf8");
    } catch (caught) {
      ariaSnapshotError = caught instanceof Error ? caught.message : String(caught);
    }
    await page.screenshot({ path: screenshotPath, fullPage: scenario !== "terminal_bridge" });
    const emptyPage = isEmptyPage(uxSignals);
    checks = {
      console_error_count: consoleErrors.length,
      page_error_count: pageErrors.length,
      horizontal_overflow: !Boolean(overflow?.ok),
      empty_page: emptyPage,
      ...geometry,
      ...fixture,
    };
    ok = consoleErrors.length === 0 && pageErrors.length === 0 && Boolean(overflow?.ok)
      && !emptyPage
      && Object.values(geometry).filter((value) => typeof value === "boolean").every(Boolean)
      && Object.values(fixture).every(Boolean);
    if (emptyPage) {
      error = `Leere Seite: ${uxSignals?.interactiveControlCount ?? 0} Bedienelemente, `
        + `${uxSignals?.renderedTextLength ?? 0} Zeichen Text (Boden ${EMPTY_PAGE_TEXT_FLOOR}). `
        + "Nicht fertig gerendert — die Evidenz dieses Viewports ist wertlos.";
    }
  } catch (caught) {
    ok = false;
    error = caught instanceof Error ? caught.message : String(caught);
    try {
      await page.screenshot({ path: screenshotPath, fullPage: scenario !== "terminal_bridge" });
    } catch {
      // Best-effort failure artifact only.
    }
  } finally {
    await context.close().catch(() => {});
  }

  return {
    route,
    url: routeUrl(baseUrl, route),
    viewport,
    ok,
    screenshotPath,
    ariaSnapshotPath: ariaSnapshotError ? null : ariaSnapshotPath,
    ariaSnapshotError,
    consoleErrors,
    pageErrors,
    overflow,
    uxSignals,
    checks,
    status: ok ? "passed" : "failed",
    screenshot: path.basename(screenshotPath),
    viewport: viewport.name,
    viewportSize: { width: viewport.width, height: viewport.height },
    error,
  };
}

// --interactive: Klick-/Dialog-Check fuer den Buzz-Agenten-Cleanup-Dialog,
// portiert aus dem Ad-hoc-Skript /tmp/buzz-dialog-check.mjs des Laufs
// t_626713b9 (2026-08-01). Artefakte: <outputDir>/dialog-interactive/ mit
// dialog-<viewport>.png und dialog-check-summary.json. Der finale Start wird
// bewusst NICHT geklickt (wuerde Agenten neu starten) — der Dialog wird
// abgebrochen, final_start_clicked bleibt false.
const DIALOG_INTERACTIVE_VIEWPORTS = [
  { name: "mobile-390", width: 390, height: 844 },
  { name: "desktop-1366", width: 1366, height: 900 },
];
const DIALOG_WARNING =
  "Laufende Antworten können verloren gehen. Jeder Agent kann rund 45 Sekunden Nachrichten verpassen.";

async function runDialogInteractionCheck(browser, baseUrl, outputDir, route) {
  const dialogDir = path.join(outputDir, "dialog-interactive");
  await fs.mkdir(dialogDir, { recursive: true });
  const checks = [];
  for (const viewport of DIALOG_INTERACTIVE_VIEWPORTS) {
    const check = { viewport: viewport.name, passed: true, findings: [], console_errors: [] };
    const fail = (message) => {
      check.passed = false;
      check.findings.push(message);
    };
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    try {
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error") check.console_errors.push(message.text());
      });
      page.on("pageerror", (error) => check.console_errors.push(String(error)));

      await page.goto(routeUrl(baseUrl, route), { waitUntil: "networkidle", timeout: CONNECT_TIMEOUT_MS });
      const startButton = page.getByRole("button", { name: "Agenten aufräumen" });
      await startButton.waitFor({ state: "visible", timeout: 20_000 });
      if (!(await startButton.isEnabled())) fail("Start-Knopf auf Leiste nicht aktiv");

      await startButton.click();
      const dialog = page.getByRole("dialog", { name: "Buzz-Agenten aufräumen bestätigen" });
      await dialog.waitFor({ state: "visible", timeout: 10_000 });

      if (!(await dialog.getByText("Buzz-Agenten aufräumen?").count())) {
        fail("Dialog-Titel fehlt");
      }

      const targetsHeadline = await dialog
        .locator("p", { hasText: "Ziele · seriell" })
        .first()
        .textContent();
      const expectedCount = Number((targetsHeadline || "").match(/(\d+)\s+Ziele/)?.[1]);
      const listItems = await dialog.locator("ul li").allTextContents();
      if (!expectedCount || listItems.length !== expectedCount) {
        fail(`Zielanzahl passt nicht: Kopf=${targetsHeadline}, Liste=${listItems.length}`);
      }
      check.targets = listItems;

      if (!(await dialog.getByText(DIALOG_WARNING).count())) {
        fail("Warnwortlaut (AC-4) nicht wörtlich gefunden");
      }

      const confirmButton = dialog.getByRole("button", { name: "Start bestätigen" });
      const cancelButton = dialog.getByRole("button", { name: "Abbrechen" });
      if (await confirmButton.isEnabled()) fail("Start bestätigen vor Checkbox aktiv");

      for (const [label, locator] of [
        ["Start bestätigen", confirmButton],
        ["Abbrechen", cancelButton],
        ["Checkbox-Zeile", dialog.locator("label", { hasText: "Warnung gelesen" })],
      ]) {
        const box = await locator.first().boundingBox();
        if (!box || box.height < 44 || box.width < 44) {
          fail(`Touchziel ${label} zu klein: ${JSON.stringify(box)}`);
        }
      }

      await dialog.getByText("Ich habe die Warnung gelesen.").click();
      if (!(await confirmButton.isEnabled())) {
        fail("Start bestätigen nach Checkbox weiterhin gesperrt");
      }

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      );
      if (overflow > 0) fail(`Horizontaler Overflow: ${overflow}px`);

      await page.screenshot({
        path: path.join(dialogDir, `dialog-${viewport.name}.png`),
        fullPage: false,
      });

      await cancelButton.click();
      await dialog.waitFor({ state: "detached", timeout: 5_000 }).catch(async () => {
        if (await dialog.count()) fail("Dialog nach Abbrechen noch offen");
      });

      if (check.console_errors.length) fail("Konsolenfehler vorhanden");
    } catch (caught) {
      fail(`Ausnahme: ${String(caught).slice(0, 500)}`);
    } finally {
      await context.close().catch(() => {});
    }
    checks.push(check);
  }
  const summary = {
    base_url: baseUrl,
    warning_wortlaut: DIALOG_WARNING,
    final_start_clicked: false,
    checks,
    passed: checks.every((entry) => entry.passed),
  };
  const summaryPath = path.join(dialogDir, "dialog-check-summary.json");
  await fs.writeFile(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  process.stdout.write(`${summaryPath}\n`);
  return summary.passed;
}

async function main() {
  const { baseUrl, outputDir, gitHead, routes, viewports, scenario, interactive } = parseArgs(process.argv);
  await fs.mkdir(outputDir, { recursive: true });
  const chromium = requirePlaywrightChromium();
  const browser = await chromium.launch({
    executablePath: resolveChromiumExecutable(),
    headless: true,
    args: ["--no-sandbox"],
  });
  const results = [];
  let interactivePassed = true;
  try {
    for (const route of routes) {
      for (const viewport of viewports) {
        results.push(await checkOne(browser, baseUrl, outputDir, route, viewport, scenario));
      }
    }
    if (interactive) {
      interactivePassed = await runDialogInteractionCheck(browser, baseUrl, outputDir, routes[0]);
    }
  } finally {
    await browser.close().catch(() => {});
  }
  const summary = {
    ok: results.every((result) => result.ok),
    allPassed: results.every((result) => result.ok),
    scenario,
    generatedAt: new Date().toISOString(),
    gitHead,
    baseUrl,
    routes,
    viewports: viewports.map(({ name, width, height }) => ({ name, width, height })),
    results,
  };
  const summaryPath = path.join(outputDir, "summary.json");
  await fs.writeFile(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  process.stdout.write(`${summaryPath}\n`);
  return summary.ok && interactivePassed ? 0 : 1;
}

process.exitCode = await main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  return 1;
});
