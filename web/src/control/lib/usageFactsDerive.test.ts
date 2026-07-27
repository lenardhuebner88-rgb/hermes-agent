import { describe, expect, it } from "vitest";
import type { UsageFactsGroup } from "../hooks/usageFacts";
import {
  AUX_ORIGIN,
  foldAuxGroups,
  foldGroupsByModel,
  foldGroupsByOrigin,
  share,
} from "./usageFactsDerive";

const group = (
  origin: string,
  model: string | null,
  overrides: Partial<{
    factRows: number;
    context: number | null;
    contextStatus: "exact" | "lower_bound" | "unavailable";
    newInput: number | null;
    cacheRead: number | null;
    output: number | null;
  }> = {},
): UsageFactsGroup => ({
  key: {
    origin,
    profile: null,
    lane: `${origin}-lane`,
    model,
    model_label: model ?? "nicht_zuordenbar",
  },
  fact_rows: overrides.factRows ?? 1,
  tokens: {
    input_semantics: origin === "claude_code" ? "cache_exclusive" : "cache_inclusive",
    context_input: {
      tokens: overrides.context ?? null,
      status: overrides.contextStatus ?? "exact",
    },
    new_input: { tokens: overrides.newInput ?? null, status: "exact" },
    cache_read_tokens: overrides.cacheRead ?? null,
    cache_write_tokens: null,
    output_tokens: overrides.output ?? null,
  },
  billing: {},
});

describe("foldGroupsByOrigin", () => {
  it("summiert normalisierte Felder je Origin und sortiert nach Kontext-Input", () => {
    const folds = foldGroupsByOrigin([
      group("codex_cli", "m-b", { context: 900, cacheRead: 810, output: 20 }),
      group("claude_code", "m-a", { context: 700, cacheRead: 690, output: 30, factRows: 2 }),
      group("claude_code", "m-a", { context: 300, cacheRead: 294, output: 10 }),
    ]);
    expect(folds.map((fold) => fold.origin)).toEqual(["claude_code", "codex_cli"]);
    expect(folds[0]).toMatchObject({
      factRows: 3,
      contextInput: 1000,
      cacheRead: 984,
      output: 40,
      hasLowerBounds: false,
    });
  });

  it("markiert Lower-Bounds und zählt fehlende Buckets nicht als erfundene Null", () => {
    const folds = foldGroupsByOrigin([
      group("claude_code", "m-a", { context: 500, contextStatus: "lower_bound" }),
      group("claude_code", "m-b", { context: null, contextStatus: "unavailable" }),
    ]);
    expect(folds).toHaveLength(1);
    expect(folds[0].hasLowerBounds).toBe(true);
    expect(folds[0].contextInput).toBe(500);
  });
});

describe("foldGroupsByModel", () => {
  it("lässt modell-lose Gruppen dem unattributed-Topf (kein Doppelzählen)", () => {
    const { rows } = foldGroupsByModel([
      group("grok_cli", null, { context: 500, factRows: 583 }),
      group("claude_code", "m-a", { context: 1000 }),
    ]);
    expect(rows.map((row) => row.label)).toEqual(["m-a"]);
  });

  it("kappt auf das Limit und fasst den Rest zusammen", () => {
    const groups = Array.from({ length: 12 }, (_, index) =>
      group("claude_code", `m-${index}`, { context: 100 - index }),
    );
    const { rows, restRows, restContextInput } = foldGroupsByModel(groups, 10);
    expect(rows).toHaveLength(10);
    expect(restRows).toBe(2);
    expect(restContextInput).toBe(179);
  });
});

describe("foldAuxGroups", () => {
  it("faltet nur den Aux-Origin der Vertragsvokabular origin-input.v1", () => {
    const fold = foldAuxGroups([
      group(AUX_ORIGIN, "m-a", { context: 100, factRows: 12 }),
      group("claude_code", "m-a", { context: 1000 }),
    ]);
    expect(fold).toMatchObject({ factRows: 12, contextInput: 100 });
  });

  it("meldet leere Aux-Erfassung als 0 Zeilen, nicht als Null-Verbrauch", () => {
    expect(foldAuxGroups([])).toMatchObject({ factRows: 0, contextInput: 0 });
  });
});

describe("share", () => {
  it("gibt bei leerem Nenner null zurück statt einer erfundenen 0", () => {
    expect(share(5, 0)).toBeNull();
    expect(share(1, 4)).toBe(0.25);
  });
});
