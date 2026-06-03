import { renderToStaticMarkup } from "react-dom/server";
import { describe, it, expect } from "vitest";
import { Markdown } from "./Markdown";

function render(body: string): string {
  return renderToStaticMarkup(<Markdown body={body} />);
}

describe("Markdown", () => {
  it("renders empty-body fallback text", () => {
    expect(render("")).toContain("Keine Beschreibung.");
    expect(render("   ")).toContain("Keine Beschreibung.");
  });

  it("renders h1 as <h1>", () => {
    const html = render("# Heading One");
    expect(html).toContain("<h1");
    expect(html).toContain("Heading One");
  });

  it("renders h2 and h3", () => {
    const html = render("## Sub\n### Sub Sub");
    expect(html).toContain("<h2");
    expect(html).toContain("<h3");
    expect(html).toContain("Sub Sub");
  });

  it("renders unordered list as <ul><li>", () => {
    const html = render("- Alpha\n- Beta\n- Gamma");
    expect(html).toContain("<ul");
    expect(html).toContain("<li");
    expect(html).toContain("Alpha");
    expect(html).toContain("Gamma");
  });

  it("renders ordered list as <ol><li>", () => {
    const html = render("1. First\n2. Second");
    expect(html).toContain("<ol");
    expect(html).toContain("<li");
    expect(html).toContain("First");
    expect(html).toContain("Second");
  });

  it("renders fenced code block as <pre><code>", () => {
    const html = render("```ts\nconst x = 1;\n```");
    expect(html).toContain("<pre");
    expect(html).toContain("<code");
    expect(html).toContain("const x = 1;");
  });

  it("renders inline code via formatInline", () => {
    const html = render("Run `npm install` now.");
    expect(html).toContain("<code");
    expect(html).toContain("npm install");
  });

  it("renders **bold** as <strong>", () => {
    const html = render("This is **bold** text.");
    expect(html).toContain("<strong");
    expect(html).toContain("bold");
  });

  it("renders *italic* as <em>", () => {
    const html = render("This is *italic* text.");
    expect(html).toContain("<em");
    expect(html).toContain("italic");
  });

  it("renders links as <a> with href", () => {
    const html = render("See [docs](https://example.com).");
    expect(html).toContain('<a ');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain("docs");
  });

  it("renders blockquote as <blockquote>", () => {
    const html = render("> Important note.");
    expect(html).toContain("<blockquote");
    expect(html).toContain("Important note.");
  });

  it("renders pipe table as <table>", () => {
    const html = render("| Col A | Col B |\n| --- | --- |\n| v1 | v2 |");
    expect(html).toContain("<table");
    expect(html).toContain("<th");
    expect(html).toContain("Col A");
    expect(html).toContain("v1");
  });

  it("renders a typical FO backlog item body", () => {
    const body = `## Ziel
Einen Einkaufslisten-Tab bauen.

## Akzeptanzkriterien
- Neue Liste anlegen
- Items hinzufügen

## Gate
\`npm run gate:e2e\`

| Feld | Wert |
| --- | --- |
| Owner | piet |`;

    const html = render(body);
    expect(html).toContain("Ziel");
    expect(html).toContain("Akzeptanzkriterien");
    expect(html).toContain("Neue Liste anlegen");
    expect(html).toContain("npm run gate:e2e");
    expect(html).toContain("Owner");
    expect(html).toContain("<table");
  });
});
