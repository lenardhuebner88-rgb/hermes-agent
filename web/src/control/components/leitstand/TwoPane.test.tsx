// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TwoPane } from "./TwoPane";

describe("TwoPane", () => {
  afterEach(cleanup);
  beforeEach(() => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: true,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    });
  });

  it("collapses to one column without detail or idle content", () => {
    const { container } = render(
      <TwoPane list={<button type="button">Listenzeile</button>} detailLabel="Task-Details" />,
    );

    expect(container.querySelector('[data-layout="single"]')).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Task-Details" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Detail schließen" })).toBeNull();
  });

  it("renders an accessible detail region and closes through the callback", () => {
    const onCloseDetail = vi.fn();
    render(
      <TwoPane
        list={<button type="button">Task Alpha</button>}
        detail={<p>Alpha-Detail</p>}
        idleDetail={<p>Idle-Detail</p>}
        detailLabel="Task-Details"
        onCloseDetail={onCloseDetail}
      />,
    );

    const region = screen.getByRole("region", { name: "Task-Details" });
    expect(region.id).toBeTruthy();
    expect(screen.getByText("Alpha-Detail")).toBeTruthy();
    expect(screen.queryByText("Idle-Detail")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Detail schließen" }));
    expect(onCloseDetail).toHaveBeenCalledTimes(1);
  });

  it("restores focus to the last focused list trigger", () => {
    render(
      <TwoPane
        list={<button type="button">Task Alpha</button>}
        detail={<p>Alpha-Detail</p>}
        detailLabel="Task-Details"
        onCloseDetail={() => {}}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Task Alpha" });
    trigger.focus();
    fireEvent.focus(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Detail schließen" }));

    expect(document.activeElement).toBe(trigger);
  });

  it("uses idle content as a real second pane without inventing a close control", () => {
    render(
      <TwoPane
        list={<p>Liste</p>}
        idleDetail={<p>Aktive Ketten</p>}
        detailLabel="Aktive Kette"
      />,
    );

    expect(screen.getByRole("region", { name: "Aktive Kette" })).toBeTruthy();
    expect(screen.getByText("Aktive Ketten")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Detail schließen" })).toBeNull();
  });

  it("renders only the list below 1024px even when detail was provided", () => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    render(
      <TwoPane list={<p>Liste</p>} detail={<p>Detail</p>} detailLabel="Task-Details" />,
    );

    expect(screen.getByText("Liste")).toBeTruthy();
    expect(screen.queryByText("Detail")).toBeNull();
    expect(screen.queryByRole("region", { name: "Task-Details" })).toBeNull();
  });
});
