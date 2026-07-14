// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BoardSwitcher } from "./BoardIdentity";

afterEach(cleanup);

describe("BoardSwitcher project truth", () => {
  it("shows exactly the active project-bound boards", () => {
    render(
      <BoardSwitcher
        boards={[
          { slug: "default", name: "Hermes Agent", archived: false, project_bound: true },
          { slug: "health-track", name: "Health Track", archived: false, project_bound: true },
          { slug: "internal-test", name: "Internal Test", archived: false, project_bound: false },
          { slug: "old-project", name: "Old Project", archived: true, project_bound: true },
        ]}
        current="default"
        selected={null}
        onSelect={vi.fn()}
      />,
    );

    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Hermes Agent · aktuell", "Health Track"]);
    expect(screen.queryByText("Internal Test")).toBeNull();
  });

  it("does not leak an unbound global current board into the options", () => {
    render(
      <BoardSwitcher
        boards={[
          { slug: "default", name: "Hermes Agent", archived: false, project_bound: true },
          { slug: "health-track", name: "Health Track", archived: false, project_bound: true },
          { slug: "internal-test", name: "Internal Test", archived: false, project_bound: false },
        ]}
        current="internal-test"
        selected="default"
        onSelect={vi.fn()}
      />,
    );

    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Hermes Agent", "Health Track"]);
    expect(screen.queryByText(/Internal Test/)).toBeNull();
  });

  it("keeps board navigation usable when project metadata is unavailable", () => {
    render(
      <BoardSwitcher
        boards={[
          { slug: "default", name: "Hermes Agent", archived: false, project_bound: false },
          { slug: "health-track", name: "Health Track", archived: false, project_bound: false },
          { slug: "old", name: "Old", archived: true, project_bound: false },
        ]}
        current="default"
        selected={null}
        onSelect={vi.fn()}
      />,
    );

    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Hermes Agent · aktuell", "Health Track"]);
  });
});
