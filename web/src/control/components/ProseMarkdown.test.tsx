import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProseMarkdown } from "./ProseMarkdown";
import { pickAnswer } from "../views/ResearchView";

describe("ProseMarkdown (Phase C)", () => {
  it("rendert GFM: Überschrift, Tabelle, Code-Block", () => {
    const md = "# Befund\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nprint(1)\n```";
    const html = renderToStaticMarkup(<ProseMarkdown>{md}</ProseMarkdown>);
    expect(html).toContain("<h1>Befund</h1>");
    expect(html).toContain("<table>");
    expect(html).toContain("<td>1</td>");
    expect(html).toContain("print(1)");
    expect(html).toContain("hc-prose");
  });

  it("interpretiert HTML NIE — Script-Tags erscheinen als Text, nicht als Element", () => {
    const md = 'Hallo <script>alert("x")</script> <img src=x onerror=alert(1)>';
    const html = renderToStaticMarkup(<ProseMarkdown>{md}</ProseMarkdown>);
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    // escaped als sichtbarer Text
    expect(html).toContain("&lt;script&gt;");
  });

  it("öffnet Links extern mit noopener", () => {
    const html = renderToStaticMarkup(
      <ProseMarkdown>{"[Quelle](https://example.com)"}</ProseMarkdown>,
    );
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('href="https://example.com"');
  });
});

describe("pickAnswer (Research-Antwortwahl)", () => {
  it("nimmt den letzten Kommentar (Receipt-Muster), sonst result, sonst null", () => {
    expect(
      pickAnswer({
        task: { id: "t", title: "q", status: "done", result: "fallback" },
        comments: [
          { author: "research", body: "erster", created_at: 1 },
          { author: "research", body: "## Antwort\nfertig", created_at: 2 },
        ],
      })?.body,
    ).toBe("## Antwort\nfertig");
    expect(
      pickAnswer({ task: { id: "t", title: "q", status: "done", result: "nur result" }, comments: [] })?.body,
    ).toBe("nur result");
    expect(pickAnswer({ task: { id: "t", title: "q", status: "running" }, comments: [] })).toBeNull();
  });
});
