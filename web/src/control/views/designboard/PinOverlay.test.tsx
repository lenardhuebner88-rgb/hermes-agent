// @vitest-environment jsdom
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { PinOverlay } from "./PinOverlay";

afterEach(cleanup);

describe("PinOverlay", () => {
  it("renders existing pins", () => {
    render(<PinOverlay src="/x.png" editable={false}
      pins={[{ id: "p1", x: 0.5, y: 0.5, note: "gap" }]} />);
    expect(screen.getByTestId("pin-p1")).toBeTruthy();
  });

  it("emits normalized coords on click when editable", () => {
    const onAddPin = vi.fn();
    render(<PinOverlay src="/x.png" editable pins={[]} onAddPin={onAddPin} />);
    const surface = screen.getByTestId("pin-surface");
    surface.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100 }) as DOMRect;
    fireEvent.click(surface, { clientX: 100, clientY: 50 });
    expect(onAddPin).toHaveBeenCalledWith({ x: 0.5, y: 0.5 });
  });
});
