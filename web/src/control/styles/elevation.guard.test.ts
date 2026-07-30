import { readFileSync, readdirSync } from "node:fs";
import { extname, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CONTROL_ROOT = resolve(import.meta.dirname, "..");
const ELEVATION_CONTRACT =
  "Elevation has exactly three token steps (theme.css --shadow-raised/-floating/-overlay): " +
  "cards/inset → shadow-raised, drawer/popover/menu → shadow-floating, sheet/dialog → shadow-overlay. " +
  "Raw Tailwind shadow steps (shadow-2xl/xl/lg) and hand-tinted shadow colours (shadow-black/*) bypass the ladder — " +
  "DESIGN.md: do not invent a fourth step.";

// All former foreign offenders (lanes/ModelSelect, autoresearch/ProposalQueue,
// autoresearch/panels) were swept to token steps on 2026-07-30 (W-UIUX I12/I17).
// Keep this list EMPTY: do NOT add entries — fix the call site instead.
const FOREIGN_OFFENDER_ALLOWLIST: Record<string, string> = {};

const RAW_SHADOW_PATTERN = /\bshadow-(?:2xl|xl|lg|black)\b/;

type SourceFile = { path: string; source: string };

function productionTsxFiles(): SourceFile[] {
  const files: SourceFile[] = [];
  const walk = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      const controlPath = relative(CONTROL_ROOT, path);
      if (entry.isDirectory()) {
        walk(path);
      } else if (
        extname(entry.name) === ".tsx" &&
        !/\.(?:test|spec|stories)\./.test(entry.name)
      ) {
        files.push({ path: controlPath, source: readFileSync(path, "utf8") });
      }
    }
  };
  walk(CONTROL_ROOT);
  return files.sort((a, b) => a.path.localeCompare(b.path));
}

describe("Control elevation ladder", () => {
  it("keeps raw Tailwind shadow steps / shadow tints out of src/control .tsx", () => {
    const violations: string[] = [];
    const staleAllowlist = new Map(Object.entries(FOREIGN_OFFENDER_ALLOWLIST));
    for (const file of productionTsxFiles()) {
      if (!RAW_SHADOW_PATTERN.test(file.source)) continue;
      if (staleAllowlist.delete(file.path)) continue; // documented foreign offender
      const lines = file.source
        .split("\n")
        .map((line, index) => ({ line, index }))
        .filter(({ line }) => RAW_SHADOW_PATTERN.test(line))
        .map(({ line, index }) => `  ${file.path}:${index + 1}: ${line.trim()}`);
      violations.push(...lines);
    }
    expect(
      violations,
      `${violations.join("\n")}\n${ELEVATION_CONTRACT}`,
    ).toEqual([]);
    // An allowlist entry whose offender was fixed must be removed — otherwise
    // the list silently rots back into a licence.
    expect(
      [...staleAllowlist.keys()],
      `allowlist entries with no remaining offender (remove them): ${[...staleAllowlist.keys()].join(", ")}`,
    ).toEqual([]);
  });
});
