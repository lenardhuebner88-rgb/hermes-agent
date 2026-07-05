import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export function requirePlaywrightChromium() {
  const require = createRequire(import.meta.url);
  const candidates = [
    path.resolve(process.cwd(), "node_modules", "playwright-core"),
    path.resolve(process.cwd(), "web", "node_modules", "playwright-core"),
    path.resolve(import.meta.dirname, "..", "..", "node_modules", "playwright-core"),
  ];

  for (const candidate of candidates) {
    try {
      return require(candidate).chromium;
    } catch {
      // Try the next repo-local install before falling back to Node resolution.
    }
  }
  return require("playwright-core").chromium;
}

export function resolveChromiumExecutable() {
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
