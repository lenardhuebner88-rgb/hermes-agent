import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ModelPicker } from "./ModelPicker";

// Phase C (Programm 3): ModelPicker rendert im Token-Vokabular der Route,
// nicht im Legacy-Compat-Vokabular aus control-tokens.css.
describe("ModelPicker (Render)", () => {
  it("nutzt das bindende Token-Vokabular und kein Legacy-Vokabular", () => {
    const html = renderToStaticMarkup(<ModelPicker value="" onChange={() => {}} />);

    // Gewünschte Kontrollflächen-Sprache (siehe DESIGN.md Regel 9).
    expect(html).toContain("bg-surface-2");
    expect(html).toContain("border-line");
    expect(html).toContain("rounded-card");
    expect(html).toContain("text-ink");
    expect(html).toContain("font-data");
    expect(html).toContain("placeholder:text-ink-3");

    // Verbotenes Alt-Vokabular.
    for (const legacy of ["hc-mono", "bg-black", "text-white", "rounded-md", "--hc-border"]) {
      expect(html).not.toContain(legacy);
    }
  });

  it("behält datalist-Vorschläge und den zugänglichen Namen bei", () => {
    const html = renderToStaticMarkup(
      <ModelPicker value="" onChange={() => {}} label="Modell (leer = Profil-Default)" />,
    );
    expect(html).toContain("aria-label=\"Modell (leer = Profil-Default)\"");
    expect(html).toContain("<datalist");
    expect(html).toContain("claude-opus-4-8");
  });
});
