import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StatusChip } from "./StatusChip";

function TestIcon({ className }: { className?: string }) {
  return <svg className={className} aria-hidden />;
}

function renderTone(tone: "cyan" | "emerald") {
  return renderToStaticMarkup(
    <StatusChip icon={TestIcon} label="Stand" value="Bereit" tone={tone} />,
  );
}

describe("StatusChip", () => {
  it("keeps the legacy cyan tone in the quiet brand channel", () => {
    const html = renderTone("cyan");

    expect(html).toContain("background-color:var(--color-brand)");
    expect(html).toContain("border-brand/25");
    expect(html).toContain(
      "background-color:color-mix(in srgb, var(--color-brand) 12%, var(--color-surface-2))",
    );
    expect(html).not.toContain("var(--color-live)");
    expect(html).not.toContain("border-live");
    expect(html).not.toContain("bg-live");
  });

  it("leaves the semantic ok tone on the status channel", () => {
    const html = renderTone("emerald");

    expect(html).toContain("background-color:var(--color-status-ok)");
    expect(html).toContain("border-status-ok/25");
    expect(html).toContain(
      "background-color:color-mix(in srgb, var(--color-status-ok) 15%, var(--color-surface-2))",
    );
  });
});
