#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("../node_modules/playwright");

const PACK = "loops-date-audit";
const STATE = `/home/piet/.hermes/loops/${PACK}`;
const HEARTBEAT = path.join(STATE, "heartbeat.json");
const STORAGE = "/home/piet/.hermes/agent-browser/hermes-dashboard-storage-state.json";
const OUT = path.join(__dirname, "date-matrix");
const NOW = Date.now();
const validCurrent = { phase: "build", engine: "xai", model: "grok-4.5", started_at: new Date(NOW - 65_000).toISOString(), timeout: 3600, round: 1 };
const validLast = { phase: "plan", engine: "codex", model: "gpt-5.6-sol", secs: 12, rc: 0, at: new Date(NOW - 120_000).toISOString(), round: 1 };

const cases = [
  ["absent", { current: { ...validCurrent, started_at: undefined }, last: [validLast] }],
  ["empty", { current: { ...validCurrent, started_at: "" }, last: [validLast] }],
  ["garbage", { current: { ...validCurrent, started_at: "not-a-date" }, last: [validLast] }],
  ["epoch-zero", { current: { ...validCurrent, started_at: 0 }, last: [validLast] }],
  ["future-one-hour", { current: { ...validCurrent, started_at: new Date(NOW + 3_600_000).toISOString() }, last: [validLast] }],
  ["milliseconds-number", { current: { ...validCurrent, started_at: NOW - 65_000 }, last: [validLast] }],
  ["negative-duration", { current: validCurrent, last: [{ ...validLast, secs: -7 }] }],
  ["timezone-less", { current: { ...validCurrent, started_at: new Date(NOW - 65_000).toISOString().replace(/Z$/, "") }, last: [validLast] }],
  ["utc-z", { current: validCurrent, last: [validLast] }],
  ["plus-02", { current: { ...validCurrent, started_at: new Date(NOW - 65_000 + 7_200_000).toISOString().replace("Z", "+02:00") }, last: [validLast] }],
];

async function main() {
  await fsp.mkdir(OUT, { recursive: true });
  const original = await fsp.readFile(HEARTBEAT, "utf8").catch(() => null);
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ storageState: STORAGE, viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const results = [];
  try {
    await page.goto("http://127.0.0.1:9119/control/loops", { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "Loops", exact: true }).waitFor();
    for (const [name, heartbeat] of cases) {
      await fsp.writeFile(HEARTBEAT, `${JSON.stringify(heartbeat)}\n`);
      await page.waitForTimeout(5500);
      const api = await page.evaluate(async (pack) => {
        const response = await fetch("/api/loops", { headers: { "X-Hermes-Session-Token": window.__HERMES_SESSION_TOKEN__ } });
        const payload = await response.json();
        return { status: response.status, pack: payload.packs.find((item) => item.name === pack) ?? null };
      }, PACK);
      const card = page.getByText(PACK, { exact: true }).locator("xpath=ancestor::section[1]").last();
      const screenshot = path.join(OUT, `${name}.png`);
      await card.screenshot({ path: screenshot });
      results.push({ case: name, heartbeat, api, dom: await card.innerText(), screenshot });
    }
  } finally {
    if (original === null) await fsp.rm(HEARTBEAT, { force: true });
    else await fsp.writeFile(HEARTBEAT, original);
    await browser.close();
  }
  const output = path.join(OUT, "matrix.json");
  await fsp.writeFile(output, `${JSON.stringify(results, null, 2)}\n`);
  process.stdout.write(`${output}\n`);
  for (const row of results) process.stdout.write(`${row.case}\t${JSON.stringify(row.dom.split("\n").filter(Boolean).slice(0, 14))}\n`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
