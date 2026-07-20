/**
 * useSystemStats — S6.4c: Sparkline-Pfad-Generatoren aus Prozentwerten.
 */
import { describe, expect, it } from "vitest";

import { sparkAreaPath, sparkLinePath } from "./useSystemStats";

describe("sparkLinePath", () => {
  it("0 % → Linie am unteren Rand (y=22)", () => {
    expect(sparkLinePath(0)).toBe("M0 22 L100 22");
  });

  it("100 % → Linie am oberen Rand (y=0)", () => {
    expect(sparkLinePath(100)).toBe("M0 0 L100 0");
  });

  it("50 % → Linie in der Mitte (y=11)", () => {
    expect(sparkLinePath(50)).toBe("M0 11 L100 11");
  });

  it("clamp: negative Werte → 0", () => {
    expect(sparkLinePath(-10)).toBe("M0 22 L100 22");
  });

  it("clamp: Werte > 100 → 100", () => {
    expect(sparkLinePath(150)).toBe("M0 0 L100 0");
  });
});

describe("sparkAreaPath", () => {
  it("schließt die Fläche zum unteren Rand", () => {
    expect(sparkAreaPath(50)).toBe("M0 11 L100 11 L100 22 L0 22 Z");
  });

  it("0 % → volle Fläche", () => {
    expect(sparkAreaPath(0)).toBe("M0 22 L100 22 L100 22 L0 22 Z");
  });
});
