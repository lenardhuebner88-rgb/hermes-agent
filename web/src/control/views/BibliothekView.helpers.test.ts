import { describe, expect, it } from "vitest";
import { plainMarkdownPreview } from "./BibliothekView.helpers";

describe("plainMarkdownPreview", () => {
  it("entfernt Heading-Marker am Zeilenanfang oder nach Leerzeichen", () => {
    expect(plainMarkdownPreview("## Modell-News")).toBe("Modell-News");
    expect(plainMarkdownPreview("x. ## Titel")).toBe("x. Titel");
    expect(plainMarkdownPreview("Text\n### Untertitel")).toBe("Text Untertitel");
  });

  it("entfernt wortumschließende Fett-/Kursiv-Marker", () => {
    expect(plainMarkdownPreview("**fett**")).toBe("fett");
    expect(plainMarkdownPreview("*kursiv*")).toBe("kursiv");
    expect(plainMarkdownPreview("_kursiv_")).toBe("kursiv");
  });

  it("entfernt Inline-Code-Backticks", () => {
    expect(plainMarkdownPreview("`code`")).toBe("code");
    expect(plainMarkdownPreview("nutze `npm test`")).toBe("nutze npm test");
  });

  it("wandelt Markdown-Links in ihren Label-Text um", () => {
    expect(plainMarkdownPreview("[Label](https://u)")).toBe("Label");
    expect(plainMarkdownPreview("siehe [Doku](https://example.com)")).toBe("siehe Doku");
  });

  it("flacht Whitespace auf eine einzelne Zeile zusammen", () => {
    expect(plainMarkdownPreview("a\n\nb")).toBe("a b");
    expect(plainMarkdownPreview("  viel   leer  ")).toBe("viel leer");
  });

  it("verstümmelt keine Identifier mit Unterstrichen, Bindestrichen oder Punkten", () => {
    expect(plainMarkdownPreview("t_7ab7e21a")).toBe("t_7ab7e21a");
    expect(plainMarkdownPreview("GPT-5.6")).toBe("GPT-5.6");
    expect(plainMarkdownPreview("some-id_value")).toBe("some-id_value");
  });

  it("bereinigt den realen Briefing-Preview ohne Layout-Tokens zu zerstören", () => {
    const preview =
      "OpenAI hat GPT-5.6 heute aus der Preview in den echten Rollout geschoben: ChatGPT, Codex und API starten gleichzeitig. Anthropic zieht mit einer Fable-5-Preisstaffel nach, die Batch-Workloads deutlich günstiger macht. ## Modell-News - **OpenAI — GPT-5.6 (Sol/Terra/Luna)**: GA-Rol";
    const cleaned = plainMarkdownPreview(preview);
    expect(cleaned).not.toContain("##");
    expect(cleaned).not.toContain("**");
    expect(cleaned).toContain("Modell-News");
    expect(cleaned).toContain("OpenAI");
    expect(cleaned).toContain("GPT-5.6");
  });
});
