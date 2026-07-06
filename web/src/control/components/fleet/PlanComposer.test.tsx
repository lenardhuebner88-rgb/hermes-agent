// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PlanComposer } from "./PlanComposer";
import { de } from "../../i18n/de";

const previewResponse = {
  ok: true,
  children: [
    {
      title: "Parse prose",
      assignee: "coder",
      parents: [],
    },
    {
      title: "Compile children",
      assignee: "coder",
      parents: [0],
    },
  ],
  repairs: ["slice 'Compile children': lane missing; repaired to coder"],
  warnings: ["ambiguous slice reported: 'Compile children' lacks done-when and body"],
};

describe("PlanComposer", () => {
  const fetchMock = vi.fn();
  const onIngestSuccess = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(previewResponse), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        root_task_id: "t_root",
        child_ids: ["t_a", "t_b"],
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("previews prose children, repairs, warnings, then confirms ingest", async () => {
    render(<PlanComposer onIngestSuccess={onIngestSuccess} />);

    fireEvent.change(screen.getByLabelText(de.fleet.planProseLabel), {
      target: {
        value: "# Demo\n**Goal:** Build it.\n\n## Slice: Parse prose\n- done-when: Parsed.",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: de.fleet.planCompilePreview }));

    await screen.findByText("Parse prose");
    const preview = screen.getByRole("region", { name: de.fleet.planCompilePreviewResult });
    expect(within(preview).getByText("Compile children"));
    expect(within(preview).getByText(/lane missing/));
    expect(within(preview).getByText(/ambiguous slice/));

    fireEvent.click(screen.getByRole("button", { name: de.fleet.planIngest }));

    await waitFor(() => expect(onIngestSuccess).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/plugins/kanban/planspecs/compile-preview",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/plugins/kanban/planspecs/ingest-prose",
      expect.objectContaining({ method: "POST" }),
    );
    const [, ingestOptions] = fetchMock.mock.calls[1];
    expect(JSON.parse(String(ingestOptions?.body))).toMatchObject({
      freigabe: "operator",
    });
  });

  it("sendet die Sofort-Freigabeauswahl beim Ingest", async () => {
    render(<PlanComposer onIngestSuccess={onIngestSuccess} />);

    fireEvent.change(screen.getByLabelText(de.fleet.planProseLabel), {
      target: {
        value: "# Demo\n**Goal:** Build it now.\n\n## Slice: Parse prose\n- done-when: Parsed.",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: de.fleet.planCompilePreview }));

    await screen.findByText("Parse prose");
    fireEvent.change(screen.getByLabelText(de.fleet.planFreigabeModeLabel), {
      target: { value: "sofort" },
    });
    fireEvent.click(screen.getByRole("button", { name: de.fleet.planIngest }));

    await waitFor(() => expect(onIngestSuccess).toHaveBeenCalledTimes(1));
    const [, ingestOptions] = fetchMock.mock.calls[1];
    expect(JSON.parse(String(ingestOptions?.body))).toMatchObject({
      freigabe: "sofort",
    });
  });
});
