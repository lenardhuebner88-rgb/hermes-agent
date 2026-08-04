import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Mobile-Befund 2026-08-04 (390px, Live): lange AC-IDs wie
// „AC-ER-KONTROLLPROBE" brachen aus der fixen 28px-ID-Spur von
// .fleet-plan-criteria und überlagerten den Kriteriumstext daneben.
// Die Spur muss mitwachsen (max-content) und die ID umbrechen dürfen.
describe("fleet-plan-criteria layout guard", () => {
  const css = readFileSync(new URL("./fleet.css", import.meta.url), "utf8");

  function ruleBlock(selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    expect(match, `${selector} muss in fleet.css existieren`).toBeTruthy();
    return match?.[1] ?? "";
  }

  it("ID-Spalte ist nicht mehr fix auf 28px", () => {
    const li = ruleBlock(".fleet-plan-criteria > li");
    expect(li).not.toMatch(/grid-template-columns:\s*28px\s/);
    expect(li).toMatch(/grid-template-columns:\s*minmax\(28px,\s*max-content\)\s+minmax\(0,\s*1fr\)/);
  });

  it("AC-ID darf umbrechen statt zu überlagern", () => {
    const span = ruleBlock(".fleet-plan-criteria > li > span");
    expect(span).toMatch(/overflow-wrap:\s*anywhere/);
  });
});
