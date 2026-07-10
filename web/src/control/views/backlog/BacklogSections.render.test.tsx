import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { rankedQueueWithReasons } from "../../lib/foBacklog";
import type { BacklogItem } from "../../lib/schemas";
import {
  BacklogBoard,
  BacklogHeroPanel,
  CandidateCompareStrip,
  DoneSection,
  KeyboardHelp,
  NextTaskSpotlight,
  OwnerLoadStrip,
  QueueSurface,
  QuickViewChips,
} from "./BacklogSections";

const LEGACY_CLASSES = [
  "cyan-",
  "emerald-",
  "sky-",
  "teal-",
  "zinc-",
  "slate-",
  "red-",
  "amber-",
  "hc-mono",
  "hc-dim",
  "hc-soft",
  "text-white",
  "bg-white",
  "border-white",
] as const;

function item(id: string, overrides: Partial<BacklogItem> = {}): BacklogItem {
  return {
    id,
    title: `Task ${id}`,
    status: "next",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-07-10",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Kurze Beschreibung",
    source_path: `backlog/items/${id}-task.md`,
    readiness: "ready",
    ...overrides,
  };
}

function expectNoLegacyClasses(html: string) {
  for (const legacy of LEGACY_CLASSES) expect(html).not.toContain(legacy);
}

describe("BacklogSections sheet-A render branches", () => {
  it("keeps loaded, empty, loading, and action-error output free of removed legacy classes", () => {
    const nowSec = 1_783_700_000;
    const ready = item("0001", { risk: "high" });
    const doneItems = Array.from({ length: 6 }, (_, index) => item(`done-${index}`, { status: "done" }));
    const candidates = rankedQueueWithReasons([ready, item("0002")], nowSec);

    const loaded = renderToStaticMarkup(
      <>
        <BacklogHeroPanel activeTotal={2} doneTotal={6} breakdown={{ now: 0, next: 2, in_progress: 0, blocked: 0, later: 0 }} loading={false} nowSec={nowSec} auditPrompt="audit" viewMode="queue" onViewMode={() => undefined} />
        <NextTaskSpotlight nextTask={ready} allItemsLength={2} nowSec={nowSec} commissionPrompt="prompt" />
        <CandidateCompareStrip topCandidates={candidates} detailById={{}} nowSec={nowSec} onOpen={() => undefined} />
        <OwnerLoadStrip ownerLoad={[{ owner: "claude", total: 2, highRisk: 1, stale: 0, unready: 0 }]} />
        <QuickViewChips allItemsLength={2} quickView="all" showHelp={false} onQuickView={() => undefined} onToggleHelp={() => undefined} />
        <KeyboardHelp showHelp />
        <QueueSurface loading={false} filteredActive={[ready]} nowSec={nowSec} nextTaskId="0001" activeId="0001" selectedId="0001" detailById={{}} onOpen={() => undefined} onCommission={() => undefined} commissionState={{ "0001": "error" }} />
        <BacklogBoard filteredByStatus={{ next: [ready] }} gap="gap-3" nowSec={nowSec} nextTaskId="0001" loading={false} onOpen={() => undefined} />
        <DoneSection doneItems={doneItems} showAllDone={false} nowSec={nowSec} detailById={{}} onToggleShowAll={() => undefined} onOpen={() => undefined} />
      </>,
    );
    const empty = renderToStaticMarkup(
      <>
        <NextTaskSpotlight nextTask={null} allItemsLength={1} nowSec={nowSec} />
        <QueueSurface loading={false} filteredActive={[]} nowSec={nowSec} nextTaskId={null} activeId={null} selectedId={null} detailById={{}} onOpen={() => undefined} />
        <DoneSection doneItems={[]} showAllDone={false} nowSec={nowSec} detailById={{}} onToggleShowAll={() => undefined} onOpen={() => undefined} />
      </>,
    );
    const loading = renderToStaticMarkup(
      <QueueSurface loading filteredActive={[]} nowSec={nowSec} nextTaskId={null} activeId={null} selectedId={null} detailById={{}} onOpen={() => undefined} />,
    );

    expectNoLegacyClasses(`${loaded}${empty}${loading}`);
    expect(loaded).toContain("bg-status-ok");
    expect(loaded).toContain("bg-status-warn");
    expect(loaded).toContain("bg-status-alert");
    expect(loaded).toContain("bg-ink-3");
    expect(loaded).toMatch(/<button[^>]*class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Task 0001<\/button>/);
    expect(loaded).toContain("Erledigt Queue");
    expect(loaded).toMatch(/min-h-12[^>]*>[^<]*<p[^>]*>Erledigt Queue<\/p>/);
  });
});
