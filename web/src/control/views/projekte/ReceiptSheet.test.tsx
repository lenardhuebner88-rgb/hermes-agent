// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  parseOrThrow,
  ProjectReceiptContentSchema,
  type ProjectReceiptEntry,
} from "../../lib/schemas";

const { hookState } = vi.hoisted(() => ({
  hookState: {
    data: null as import("../../lib/schemas").ProjectReceiptContent | null,
    loading: true,
    error: null as string | null,
  },
}));

vi.mock("../../hooks/useControlData", () => ({
  useProjectReceipt: vi.fn(() => ({
    data: hookState.data,
    loading: hookState.loading,
    error: hookState.error,
    errorObj: null,
    isStale: false,
    lastUpdated: null,
    reload: vi.fn(),
  })),
}));

import { ReceiptSheet } from "./ReceiptSheet";

// Feed-Zeile, wie sie der Ergebnisse-Feed/das Drawer dem Sheet übergibt
// (real shape aus dem Backend-Vertrag, getypt statt geparst wie in
// CommitsFeed.test.tsx).
const RECEIPT: ProjectReceiptEntry = {
  agent: "Codex",
  filename: "2026-07-17-b3-parser-receipt.md",
  title: "B3 coordination parser drift receipt",
  mtime: "2026-07-17T21:04:11+00:00",
  age_seconds: 12600,
  project: "hermes-infra",
  excerpt: "status: blocked",
};

// Real-shaped GET /api/projects/receipts/{agent}/{filename} body.
const REAL_CONTENT_PAYLOAD = {
  agent: "Codex",
  filename: "2026-07-17-b3-parser-receipt.md",
  title: "B3 coordination parser drift receipt",
  mtime: "2026-07-17T21:04:11+00:00",
  truncated: true,
  markdown: "## Befund\n\n- Parser-Drift bestätigt\n- Fix in `hermes_cli`\n\n**status: blocked**",
};

function parseContent(payload: unknown = REAL_CONTENT_PAYLOAD) {
  return parseOrThrow(ProjectReceiptContentSchema, payload, "test-content");
}

afterEach(() => {
  cleanup();
  hookState.data = null;
  hookState.loading = true;
  hookState.error = null;
});

describe("ReceiptSheet", () => {
  it("shows the loading skeleton before the content arrives", () => {
    hookState.loading = true;
    hookState.data = null;
    render(<ReceiptSheet receipt={RECEIPT} onClose={() => undefined} />);
    expect(document.querySelector("[aria-busy='true']")).toBeTruthy();
    // Header kommt sofort aus der Feed-Zeile (kein Warten auf den Body).
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("B3 coordination parser drift receipt");
    expect(dialog.textContent).toContain("Codex");
  });

  it("renders the fetched markdown and the truncated hint", () => {
    hookState.loading = false;
    hookState.data = parseContent();
    render(<ReceiptSheet receipt={RECEIPT} onClose={() => undefined} />);
    expect(screen.getByRole("heading", { name: "Befund" })).toBeTruthy();
    expect(screen.getByText("Parser-Drift bestätigt")).toBeTruthy();
    expect(screen.getByText("status: blocked")).toBeTruthy();
    expect(
      screen.getByText("Gekürzt — das vollständige Receipt liegt im Vault."),
    ).toBeTruthy();
  });

  it("omits the truncated hint for a complete receipt", () => {
    hookState.loading = false;
    hookState.data = parseContent({ ...REAL_CONTENT_PAYLOAD, truncated: false });
    render(<ReceiptSheet receipt={RECEIPT} onClose={() => undefined} />);
    expect(screen.getByRole("heading", { name: "Befund" })).toBeTruthy();
    expect(
      screen.queryByText("Gekürzt — das vollständige Receipt liegt im Vault."),
    ).toBeNull();
  });

  it("shows the house error state when the fetch fails", () => {
    hookState.loading = false;
    hookState.error = "404: unknown receipt";
    render(<ReceiptSheet receipt={RECEIPT} onClose={() => undefined} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Receipt konnte nicht geladen werden.")).toBeTruthy();
  });
});
