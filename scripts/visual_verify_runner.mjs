#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import { requirePlaywrightChromium, resolveChromiumExecutable } from "./lib/playwright_chromium.mjs";

const CONNECT_TIMEOUT_MS = 15_000;
const viewports = [
  { name: "mobile-390", width: 390, height: 844, isMobile: true, hasTouch: true },
  { name: "tablet-820", width: 820, height: 1180, isMobile: true, hasTouch: true },
  { name: "desktop-1366", width: 1366, height: 900, isMobile: false, hasTouch: false },
];

function usage() {
  process.stderr.write("usage: visual_verify_runner.mjs --base-url URL --output-dir DIR --git-head SHA <route> [<route>...]\n");
}

function parseArgs(argv) {
  let baseUrl = "";
  let outputDir = "";
  let gitHead = "";
  const routes = [];
  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--base-url") {
      baseUrl = argv[++index] || "";
    } else if (arg === "--output-dir") {
      outputDir = argv[++index] || "";
    } else if (arg === "--git-head") {
      gitHead = argv[++index] || "";
    } else if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(0);
    } else if (arg.startsWith("--")) {
      throw new Error(`unknown option: ${arg}`);
    } else {
      routes.push(arg);
    }
  }
  if (!baseUrl || !outputDir || !/^[0-9a-f]{40}$/.test(gitHead) || routes.length === 0) {
    usage();
    process.exit(2);
  }
  return { baseUrl, outputDir, gitHead, routes };
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
    undersizedControls: undersizedControls.slice(0, 50),
    unlabeledControls: unlabeledControls.slice(0, 50),
    truncated: undersizedControls.length > 50 || unlabeledControls.length > 50,
  };
}

async function checkOne(browser, baseUrl, outputDir, route, viewport) {
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
  let ok = true;
  let error = null;
  try {
    await page.goto(routeUrl(baseUrl, route), { waitUntil: "domcontentloaded", timeout: CONNECT_TIMEOUT_MS });
    await page.waitForLoadState("networkidle", { timeout: CONNECT_TIMEOUT_MS }).catch(() => {});
    await page.waitForTimeout(250);
    overflow = await page.evaluate(readOverflow);
    uxSignals = await page.evaluate(readUxSignals);
    try {
      const ariaSnapshot = await page.locator("body").ariaSnapshot();
      await fs.writeFile(ariaSnapshotPath, `${ariaSnapshot.trimEnd()}\n`, "utf8");
    } catch (caught) {
      ariaSnapshotError = caught instanceof Error ? caught.message : String(caught);
    }
    await page.screenshot({ path: screenshotPath, fullPage: true });
    ok = consoleErrors.length === 0 && pageErrors.length === 0 && Boolean(overflow?.ok);
  } catch (caught) {
    ok = false;
    error = caught instanceof Error ? caught.message : String(caught);
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
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
    error,
  };
}

async function main() {
  const { baseUrl, outputDir, gitHead, routes } = parseArgs(process.argv);
  await fs.mkdir(outputDir, { recursive: true });
  const chromium = requirePlaywrightChromium();
  const browser = await chromium.launch({
    executablePath: resolveChromiumExecutable(),
    headless: true,
    args: ["--no-sandbox"],
  });
  const results = [];
  try {
    for (const route of routes) {
      for (const viewport of viewports) {
        results.push(await checkOne(browser, baseUrl, outputDir, route, viewport));
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }
  const summary = {
    ok: results.every((result) => result.ok),
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
  return summary.ok ? 0 : 1;
}

process.exitCode = await main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  return 1;
});
