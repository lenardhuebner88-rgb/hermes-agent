// @vitest-environment jsdom
/**
 * useOfflineBannerHeight — M3: die gemessene Höhe des OfflineStaleBanner
 * ([data-offline-banner]) wird als --jv-banner-h auf die .jv-Wurzel gespiegelt;
 * ohne Banner bleibt sie 0px (kein Layout-Unterschied). jarvis.css zieht die
 * Variable in den Stage-Höhen ab — damit die Frag-Leiste bei sichtbarem
 * Banner nicht clippt (Browser-Abnahme via ui-shot, hier die Mechanik).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { useRef } from "react";

import { useOfflineBannerHeight } from "./useOfflineBannerHeight";

/** jsdom hat kein ResizeObserver — minimaler Noop-Ersatz. */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function Banner({ height }: { height: number }) {
  const ref = (el: HTMLDivElement | null) => {
    if (el) {
      el.getBoundingClientRect = () =>
        ({ height, top: 0, bottom: height }) as DOMRect;
    }
  };
  return <div data-offline-banner="" ref={ref} />;
}

function Host({ bannerHeight }: { bannerHeight: number | null }) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  useOfflineBannerHeight(rootRef);
  return (
    <div data-control="">
      {bannerHeight !== null ? <Banner height={bannerHeight} /> : null}
      <div className="jv" ref={rootRef} data-testid="jv-root" />
    </div>
  );
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useOfflineBannerHeight (M3)", () => {
  it("spiegelt die Banner-Höhe als --jv-banner-h auf die .jv-Wurzel", async () => {
    const { getByTestId } = render(<Host bannerHeight={37} />);

    expect(getByTestId("jv-root").style.getPropertyValue("--jv-banner-h")).toBe("37px");
  });

  it("ohne Banner bleibt die Variable 0px", () => {
    const { getByTestId } = render(<Host bannerHeight={null} />);

    expect(getByTestId("jv-root").style.getPropertyValue("--jv-banner-h")).toBe("0px");
  });

  it("Erscheinen/Verschwinden des Banners aktualisiert die Variable", async () => {
    const { getByTestId, rerender } = render(<Host bannerHeight={null} />);
    expect(getByTestId("jv-root").style.getPropertyValue("--jv-banner-h")).toBe("0px");

    rerender(<Host bannerHeight={42} />);
    await act(async () => {
      // MutationObserver-Flush abwarten (läuft nach den Microtasks).
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(getByTestId("jv-root").style.getPropertyValue("--jv-banner-h")).toBe("42px");

    rerender(<Host bannerHeight={null} />);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(getByTestId("jv-root").style.getPropertyValue("--jv-banner-h")).toBe("0px");
  });
});
