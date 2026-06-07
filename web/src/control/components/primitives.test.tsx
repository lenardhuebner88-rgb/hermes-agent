import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Disclosure, Panel, SkeletonCard, Stat, Text } from "./primitives";

/**
 * Smoke tests for the Aurora Violet primitives. The repo's component tests
 * render to static markup (react-dom/server) — there is no jsdom/testing
 * harness in this project — so we assert on the rendered HTML. For controlled
 * components (Disclosure) we render each state explicitly rather than clicking.
 */
describe("primitives", () => {
  describe("Disclosure", () => {
    it("reflects open state in aria-expanded and renders the panel when open", () => {
      const open = renderToStaticMarkup(
        <Disclosure summary={<span>Details</span>} open id="d1">
          <p>body content</p>
        </Disclosure>,
      );
      expect(open).toContain('aria-expanded="true"');
      expect(open).toContain('aria-controls="d1-panel"');
      expect(open).toContain("body content");
    });

    it("collapses aria-expanded and omits the panel when closed", () => {
      const closed = renderToStaticMarkup(
        <Disclosure summary={<span>Details</span>} open={false} id="d1">
          <p>body content</p>
        </Disclosure>,
      );
      expect(closed).toContain('aria-expanded="false"');
      expect(closed).not.toContain("body content");
    });

    it("defaults to closed via defaultOpen", () => {
      const html = renderToStaticMarkup(
        <Disclosure summary={<span>Details</span>}>
          <p>hidden body</p>
        </Disclosure>,
      );
      expect(html).toContain('aria-expanded="false"');
      expect(html).not.toContain("hidden body");
    });
  });

  describe("SkeletonCard", () => {
    it("renders the requested number of shimmer rows", () => {
      const html = renderToStaticMarkup(<SkeletonCard rows={5} />);
      // One title bar + N body rows, all .hc-skeleton blocks.
      const count = (html.match(/hc-skeleton/g) ?? []).length;
      expect(count).toBe(6); // 1 title + 5 rows
      expect(html).toContain('aria-busy="true"');
    });

    it("defaults to three rows", () => {
      const html = renderToStaticMarkup(<SkeletonCard />);
      const count = (html.match(/hc-skeleton/g) ?? []).length;
      expect(count).toBe(4); // 1 title + 3 rows
    });
  });

  describe("Panel", () => {
    it("renders eyebrow, title and actions", () => {
      const html = renderToStaticMarkup(
        <Panel eyebrow="System" title="Fleet" actions={<button>Refresh</button>}>
          <p>panel body</p>
        </Panel>,
      );
      expect(html).toContain("System");
      expect(html).toContain("Fleet");
      expect(html).toContain("Refresh");
      expect(html).toContain("panel body");
      expect(html).toContain("hc-eyebrow");
    });
  });

  describe("Stat", () => {
    it("renders label and value", () => {
      const html = renderToStaticMarkup(<Stat label="Aktiv" value="7" />);
      expect(html).toContain("Aktiv");
      expect(html).toContain("7");
    });

    it("renders an accent hero number with aurora gradient text", () => {
      const html = renderToStaticMarkup(<Stat label="Offen" value="12" accent />);
      expect(html).toContain("Offen");
      expect(html).toContain("12");
      expect(html).toContain("hc-aurora-text");
    });
  });

  describe("Text", () => {
    it("renders the named type scale class", () => {
      const html = renderToStaticMarkup(<Text variant="title">Hallo</Text>);
      expect(html).toContain("hc-type-title");
      expect(html).toContain("Hallo");
    });
  });
});
