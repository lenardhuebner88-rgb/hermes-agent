import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = readFileSync(path.resolve(import.meta.dirname, "KettenTab.tsx"), "utf8");

describe("KettenTab accessibility and runtime labels", () => {
  it("makes all interactive chain rows keyboard-focus visible", () => {
    expect(src).toMatch(/fleet-kchip[^`]*focus-visible:ring-2/);
    expect(src).toMatch(/fleet-fokus[^"]*focus-visible:ring-2/);
    expect(src).toMatch(/fleet-q[^"]*focus-visible:ring-2/);
    expect(src).toMatch(/fleet-f-row[^"]*focus-visible:ring-2/);
  });

  it("distinguishes task lane from runtime profile in visible copy", () => {
    expect(src).toContain("de.fleet.detailLabelAssignee");
    expect(src).toContain("de.fleet.detailLabelModell");
    expect(src).not.toContain("{focusNode.assignee ?? \"—\"}");
    expect(src).toMatch(/de\.fleet\.detailLabelModell\}: \{focusNode\.latest_run\.profile\}/);
  });
});
