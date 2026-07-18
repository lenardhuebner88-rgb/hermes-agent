// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  parseOrThrow,
  ProjectsReceiptsResponseSchema,
  type ProjectReceiptEntry,
} from "../../lib/schemas";

const { receiptHook } = vi.hoisted(() => ({
  receiptHook: vi.fn(() => ({
    data: null,
    loading: true,
    error: null,
    errorObj: null,
    isStale: false,
    lastUpdated: null,
    reload: vi.fn(),
  })),
}));

vi.mock("../../hooks/useControlData", () => ({
  useProjectReceipt: receiptHook,
}));

import { ReceiptsFeed } from "./ReceiptsFeed";

// Real /api/projects/receipts payload shape (Backend-Vertrag Stage 12,
// projects_overview.build_receipts_payload): ISO-mtime, project kann null
// sein (Receipt ohne Zuordnung), excerpt kann null sein (leerer/unlesbarer
// Dateikopf).
const REAL_FEED_PAYLOAD = {
  generated_at: 1784322251,
  receipts: [
    {
      agent: "Codex",
      filename: "2026-07-17-b3-parser-receipt.md",
      title: "B3 coordination parser drift receipt",
      mtime: "2026-07-17T21:04:11+00:00",
      age_seconds: 12600,
      project: "hermes-infra",
      excerpt: "status: blocked",
    },
    {
      agent: "Kimi",
      filename: "2026-07-17-projekte-feed-receipt.md",
      title: "Projekte receipts feed frontend",
      mtime: "2026-07-17T20:34:11+00:00",
      age_seconds: 14400,
      project: "unknown-slug",
      excerpt: null,
    },
    {
      agent: "Claude-Code",
      filename: "2026-07-17-ht-harvest-receipt.md",
      title: "HT harvest deploy looplaunch",
      mtime: "2026-07-17T19:04:11+00:00",
      age_seconds: 19800,
      project: null,
      excerpt: "landed",
    },
  ],
};

const NAMES = { "hermes-infra": "Hermes Infra" };
// mtime der ersten Zeile + age_seconds = fester Jetzt-Punkt → "vor 3h".
const NOW = Date.parse("2026-07-18T00:34:11+00:00") / 1000;

function parseFeed(payload: unknown = REAL_FEED_PAYLOAD): ProjectReceiptEntry[] {
  return parseOrThrow(ProjectsReceiptsResponseSchema, payload, "test").receipts;
}

function renderFeed(
  receipts: ProjectReceiptEntry[] = parseFeed(),
  opts: { error?: boolean } = {},
) {
  return render(
    <ReceiptsFeed receipts={receipts} projectNames={NAMES} now={NOW} error={opts.error} />,
  );
}

afterEach(() => cleanup());

describe("ProjectsReceiptsResponseSchema (real feed fixture)", () => {
  it("parses the frozen feed shape incl. project/excerpt nulls", () => {
    const receipts = parseFeed();
    expect(receipts).toHaveLength(3);
    expect(receipts[0].agent).toBe("Codex");
    expect(receipts[0].excerpt).toBe("status: blocked");
    expect(receipts[1].excerpt).toBeNull();
    expect(receipts[2].project).toBeNull();
  });
});

describe("ReceiptsFeed", () => {
  it("renders rows with agent badge, title, resolved project chip and age", () => {
    renderFeed();
    expect(screen.getByText("Ergebnisse")).toBeTruthy();
    expect(screen.getByText("Receipts")).toBeTruthy();
    expect(screen.getByText("Codex")).toBeTruthy();
    expect(screen.getByText("B3 coordination parser drift receipt")).toBeTruthy();
    // Projekt-Chip: aufgelöster Anzeigename wenn der Slug bekannt ist,
    // roher Slug sonst; project:null-Zeilen bekommen keinen Chip.
    expect(screen.getByText("Hermes Infra")).toBeTruthy();
    expect(screen.getByText("unknown-slug")).toBeTruthy();
    const nullProjectRow = screen
      .getByText("HT harvest deploy looplaunch")
      .closest("button");
    expect(nullProjectRow?.textContent).not.toContain("Hermes Infra");
    // Relatives Alter aus der ISO-mtime (12600s → "vor 3h").
    expect(screen.getAllByText(/vor \d+[smhd]/).length).toBeGreaterThanOrEqual(3);
  });

  it("opens the reader sheet on row click and fetches that receipt", () => {
    renderFeed();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Receipt B3 coordination parser drift receipt öffnen" }),
    );
    expect(receiptHook).toHaveBeenCalledWith("Codex", "2026-07-17-b3-parser-receipt.md");
    // Sheet ist offen (Skeleton-Zustand aus dem gemockten Hook) und trägt
    // Titel + Agent-Badge aus der Zeile.
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("B3 coordination parser drift receipt");
    expect(dialog.textContent).toContain("Codex");
    expect(document.querySelector("[aria-busy='true']")).toBeTruthy();
  });

  it("caps the list at 12 rows behind the alle-anzeigen expander", () => {
    const many = parseFeed({
      generated_at: 1784322251,
      receipts: Array.from({ length: 14 }, (_, index) => ({
        agent: "Codex",
        filename: `receipt-${index}.md`,
        title: `Receipt Nummer ${index}`,
        mtime: "2026-07-17T21:04:11+00:00",
        age_seconds: 12600,
        project: null,
        excerpt: null,
      })),
    });
    renderFeed(many);
    expect(screen.getAllByRole("button", { name: /^Receipt / })).toHaveLength(12);
    expect(screen.queryByText("Receipt Nummer 13")).toBeNull();

    const expander = screen.getByRole("button", { name: "Alle 14 anzeigen" });
    expect(expander.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expander);
    expect(screen.getAllByRole("button", { name: /^Receipt / })).toHaveLength(14);
    expect(screen.getByText("Receipt Nummer 13")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Weniger anzeigen" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("renders a calm empty state without receipts", () => {
    renderFeed([]);
    expect(
      screen.getByText("Noch keine Receipts — Agents legen sie nach abgeschlossener Arbeit im Vault ab."),
    ).toBeTruthy();
  });

  it("shows the section title and inline error when fetch failed and list is empty", () => {
    renderFeed([], { error: true });
    expect(screen.getByText("Ergebnisse")).toBeTruthy();
    expect(screen.getByText("Receipts konnten nicht geladen werden.")).toBeTruthy();
    expect(
      screen.queryByText("Noch keine Receipts — Agents legen sie nach abgeschlossener Arbeit im Vault ab."),
    ).toBeNull();
  });

  it("keeps the list visible when error is set but data is still present", () => {
    renderFeed(parseFeed(), { error: true });
    expect(screen.getByText("Ergebnisse")).toBeTruthy();
    expect(screen.getByText("B3 coordination parser drift receipt")).toBeTruthy();
    expect(screen.getByText("Codex")).toBeTruthy();
    expect(screen.queryByText("Receipts konnten nicht geladen werden.")).toBeNull();
  });
});
