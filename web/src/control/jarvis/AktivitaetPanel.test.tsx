// @vitest-environment jsdom
/**
 * AktivitaetPanel — „AKTIVITÄT" der Jarvis-Shell (S3.10): Receipts+Commits
 * über dieselben Hooks/Polling-Keys wie die Klassik (fetchJSON-Mock durch
 * den echten pollingStore, Payloads im Backend-Format). Erwartungen: Strip
 * (neuestes Receipt + Zähler), Drawer mit Tabs, Receipt-Zeile öffnet das
 * KLASSIK-Lese-Sheet (ReceiptSheet, inkl. Inhalt-Fetch), „Alle N anzeigen"-
 * Disclosure, Projekt-Namensauflösung, Empty-/Error-/Loading-States.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";

import { _resetPollingStore } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const fetchJSONMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchJSON: fetchJSONMock,
  };
});

import { AktivitaetPanel } from "./AktivitaetPanel";

const NOW = Math.floor(Date.now() / 1000);

// ── Fixtures im echten Backend-Shape (projects_overview.py) ────────────────
function fixtureReceipt(overrides: Record<string, unknown> = {}) {
  return {
    agent: "kimi",
    filename: "2026-07-19-receipt-1.md",
    title: "Shell-Einzug II gebaut",
    mtime: new Date((NOW - 300) * 1000).toISOString(),
    age_seconds: 300,
    project: "hermes-infra",
    excerpt: null,
    ...overrides,
  };
}

function fixtureCommit(overrides: Record<string, unknown> = {}) {
  return {
    project: "hermes-infra",
    project_name: "Hermes Infra",
    hash: "9d8fa62d8",
    message: "jarvis: s3.10 aktivitaet/sessions panels",
    author: "kimi",
    committed_at: NOW - 600,
    age_seconds: 600,
    attribution: null,
    ...overrides,
  };
}

function mockEndpoints({
  receipts = [fixtureReceipt()],
  commits = [fixtureCommit()],
  receiptContent,
  receiptsError,
  commitsError,
}: {
  receipts?: Array<Record<string, unknown>>;
  commits?: Array<Record<string, unknown>>;
  receiptContent?: Record<string, unknown>;
  receiptsError?: string;
  commitsError?: string;
} = {}) {
  fetchJSONMock.mockImplementation((url: string, init?: { method?: string }) => {
    if (url === "/api/projects") {
      return Promise.resolve({
        generated_at: NOW,
        registry_errors: [],
        projects: [
          {
            slug: "hermes-infra",
            name: "Hermes Infra",
            repo_path: "/home/piet/.hermes/hermes-agent",
            parent: null,
            links: [],
            last_commit: null,
            kanban: null,
            loops: null,
            errors: [],
          },
        ],
      });
    }
    if (url === "/api/projects/receipts") {
      return receiptsError
        ? Promise.reject(new Error(receiptsError))
        : Promise.resolve({ generated_at: NOW, receipts });
    }
    if (url === "/api/projects/commits") {
      return commitsError
        ? Promise.reject(new Error(commitsError))
        : Promise.resolve({ generated_at: NOW, errors: [], commits });
    }
    if (url.startsWith("/api/projects/receipts/")) {
      return Promise.resolve(
        receiptContent ?? {
          agent: "kimi",
          filename: "2026-07-19-receipt-1.md",
          title: "Shell-Einzug II gebaut",
          mtime: new Date((NOW - 300) * 1000).toISOString(),
          truncated: false,
          markdown: "## Ergebnis\n\nPanels stehen, Gates grün.",
        },
      );
    }
    return Promise.reject(new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`));
  });
}

beforeEach(() => {
  _resetPollingStore();
  mockEndpoints();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

function renderPanel(open = true) {
  return render(
    <MemoryRouter>
      <AktivitaetPanel open={open} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

/** Stateful-Harness: prüft den Strip→Drawer-Toggle über das Props-Contract. */
function renderHarness() {
  function Harness() {
    const [open, setOpen] = useState(false);
    return (
      <MemoryRouter>
        <AktivitaetPanel open={open} onToggle={() => setOpen((value) => !value)} />
      </MemoryRouter>
    );
  }
  return render(<Harness />);
}

describe("AktivitaetPanel (Receipts + Commits, echte Daten über die Bestands-Hooks)", () => {
  it("Strip zeigt neuestes Receipt und Zähler, Drawer ist zu", async () => {
    renderPanel(false);

    const tease = await screen.findByRole("button", { name: /Shell-Einzug II gebaut/ });
    expect(tease.getAttribute("title")).toBe("Shell-Einzug II gebaut");
    expect(screen.getByText("1 · 1")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "AKTIVITÄT" })).toBeNull();
  });

  it("Toggle öffnet den Drawer (Strip-Button, aria-expanded)", async () => {
    renderHarness();

    const toggle = screen.getByRole("button", {
      name: "Aktivitäts-Feed (Receipts und Commits) umschalten",
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(await screen.findByRole("region", { name: "AKTIVITÄT" })).toBeTruthy();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("Receipts-Tab: Zeilen mit Agent-Badge, aufgelöstem Projekt-Chip und Titel", async () => {
    renderPanel();

    // Drawer-Region scopen: der Strip-Teaser trägt denselben Titel.
    const drawer = within(await screen.findByRole("region", { name: "AKTIVITÄT" }));
    expect(await drawer.findByText("Shell-Einzug II gebaut")).toBeTruthy();
    expect(drawer.getByText("kimi")).toBeTruthy();
    // slug → Anzeigename (dieselbe Ableitung wie die Klassik).
    expect(drawer.getByText("Hermes Infra")).toBeTruthy();
  });

  it("Receipt-Zeile öffnet das KLASSIK-Lese-Sheet (ReceiptSheet + Inhalt-Fetch)", async () => {
    renderPanel();

    const drawer = within(await screen.findByRole("region", { name: "AKTIVITÄT" }));
    const row = await drawer.findByRole("button", {
      name: "Receipt Shell-Einzug II gebaut öffnen",
    });
    row.click();

    // DrawerShell der Klassik (role=dialog) + geladener Markdown-Body.
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(await screen.findByText(/Panels stehen, Gates grün/)).toBeTruthy();
    expect(
      fetchJSONMock.mock.calls.some(([url]) =>
        String(url).startsWith("/api/projects/receipts/kimi/"),
      ),
    ).toBe(true);
  });

  it("Disclosure Alle-N-anzeigen kappt den Feed wie die Klassik", async () => {
    mockEndpoints({
      receipts: Array.from({ length: 12 }, (_, index) =>
        fixtureReceipt({
          filename: `receipt-${index}.md`,
          title: `Receipt Nummer ${index}`,
        }),
      ),
    });
    renderPanel();

    const drawer = within(await screen.findByRole("region", { name: "AKTIVITÄT" }));
    expect(await drawer.findByText("Receipt Nummer 0")).toBeTruthy();
    expect(drawer.queryByText("Receipt Nummer 11")).toBeNull();

    const expand = drawer.getByRole("button", { name: "Alle 12 anzeigen" });
    expand.click();
    expect(await drawer.findByText("Receipt Nummer 11")).toBeTruthy();
    expect(drawer.getByRole("button", { name: "Weniger anzeigen" })).toBeTruthy();
  });

  it("Commits-Tab: Hash, Message, Attribution und Disclosure", async () => {
    mockEndpoints({
      commits: [
        fixtureCommit(),
        fixtureCommit({
          hash: "abc123def",
          message: "kanban: t_123 landen",
          attribution: { kind: "kanban", lane: "build", model: "kimi-k3", task_id: "t_123" },
        }),
        ...Array.from({ length: 9 }, (_, index) =>
          fixtureCommit({ hash: `deadbeef${index}`, message: `commit ${index}` }),
        ),
      ],
    });
    renderPanel();

    const tab = await screen.findByRole("tab", { name: /COMMITS/ });
    tab.click();

    expect(await screen.findByText("9d8fa62d8")).toBeTruthy();
    // Attribution schlägt den Autor (commitAttributionLabel der Klassik).
    expect(screen.getByText("build · kimi-k3")).toBeTruthy();
    // Cap 8 → 11 Commits hinter der Disclosure.
    expect(screen.queryByText("commit 8")).toBeNull();
    screen.getByRole("button", { name: "Alle 11 anzeigen" }).click();
    expect(await screen.findByText("commit 8")).toBeTruthy();
  });

  it("Empty-States je Tab (kein falscher Leer-Zustand beim Laden)", async () => {
    mockEndpoints({ receipts: [], commits: [] });
    renderPanel();

    expect(
      await screen.findByText(
        "Noch keine Receipts — Agents legen sie nach abgeschlossener Arbeit im Vault ab.",
      ),
    ).toBeTruthy();
    (await screen.findByRole("tab", { name: /COMMITS/ })).click();
    expect(
      await screen.findByText("Keine Commits in den registrierten Projekten gefunden."),
    ).toBeTruthy();
  });

  it("Fehler je Quelle inline (role=alert), nie still", async () => {
    mockEndpoints({ receiptsError: "network timeout", commitsError: "boom" });
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Receipts konnten nicht geladen werden.");
    (await screen.findByRole("tab", { name: /COMMITS/ })).click();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((node) => node.textContent?.includes("Commits konnten nicht geladen werden."))).toBe(
      true,
    );
  });

  it("Loading-State vor dem ersten Poll-Ergebnis", () => {
    fetchJSONMock.mockImplementation(() => new Promise(() => {}));
    renderPanel();

    expect(screen.getByText("Lade Aktivität …")).toBeTruthy();
    expect(screen.queryByText(/Noch keine Receipts/)).toBeNull();
  });
});
