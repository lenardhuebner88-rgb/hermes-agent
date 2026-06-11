import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

describe("ErrorBoundary", () => {
  it("renders children while healthy", () => {
    const html = renderToStaticMarkup(<ErrorBoundary><p>Route ok</p></ErrorBoundary>);
    expect(html).toContain("Route ok");
  });

  it("renders the German fallback after an error state", () => {
    const boundary = new ErrorBoundary({ children: <p>Route ok</p> });
    boundary.state = ErrorBoundary.getDerivedStateFromError(new Error("boom"));
    const html = renderToStaticMarkup(boundary.render());

    expect(html).toContain("Ansicht abgestürzt");
    expect(html).toContain("Neu laden");
  });
});
