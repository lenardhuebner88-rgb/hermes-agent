import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Guard: /control/flow must be LIVE — never the design-package demo data.
// (FLOW_LIVE_WIRING_ADDENDUM: flowMock must not be imported in the live view,
// and no design-package dummy run IDs may appear in the product path.)
const src = readFileSync(fileURLToPath(new URL("./FlowView.tsx", import.meta.url)), "utf8");

describe("FlowView is live-wired, not mock", () => {
  it("does not import flowMock (deleted from the product path)", () => {
    expect(src).not.toMatch(/flowMock/i);
  });

  it("contains none of the design-package demo run IDs", () => {
    for (const demo of ["REQ-142", "REQ-145", "REQ-150", "PLN-77", "PLN-81", "RUN-781", "RUN-786", "RUN-790", "CHK-58", "CHK-19", "SHIP-31", "SHIP-30"]) {
      expect(src, `demo id ${demo} leaked into FlowView`).not.toContain(demo);
    }
  });

  it("has no 'demo'/'mock' wording in the UI", () => {
    expect(src.toLowerCase()).not.toMatch(/demo-daten|mockdaten|demonstriert/);
  });

  it("reads from the live board + task-detail + worker hooks", () => {
    expect(src).toMatch(/useBoard/);
    expect(src).toMatch(/useTaskDetail/);
    expect(src).toMatch(/useHermesWorkers/);
    // Phase 2: das Board gruppiert nach Root-Ketten (lib/fleet.buildChains),
    // nicht mehr nach flachen Stage-Spalten.
    expect(src).toMatch(/buildChains/);
  });

  it("surfaces structured board source errors inside the Flow view", () => {
    expect(src).toMatch(/board\.data\?\.source_errors/);
    expect(src).toMatch(/sourceErrorTitle/);
    expect(src).toMatch(/sourceErrorContext/);
  });

  it("shows quiet operator explanations next to Flow subtask status pills", () => {
    expect(src).toMatch(/getFlowSubtaskStatusExplanation/);
    expect(src).toMatch(/c\.status === "blocked" \? c\.latest_summary : null/);
    expect(src).toMatch(/hc-dim/);
    expect(src).toMatch(/flex-wrap/);
  });

  it("renders a compact dependency-chain explanation from task-detail links", () => {
    expect(src).toMatch(/FlowChainInsight/);
    expect(src).toMatch(/detail\?\.links/);
    expect(src).toMatch(/Gehalten/);
    expect(src).toMatch(/Ready-Nachbar im Snapshot/);
    expect(src).not.toMatch(/Startbarer Snapshot-Kandidat/);
    expect(src).toMatch(/Läuft bereits/);
    expect(src).toMatch(/Direkte Verknüpfungen/);
    expect(src).toMatch(/Mögliche Vorgänger/);
    expect(src).toMatch(/Snapshot-Hinweis/);
    expect(src).toMatch(/todo ist uneindeutig/);
    expect(src).toMatch(/Snapshot-Alter/);
  });

  it("opens deliverables through authenticated blob fetches, not raw API anchors", () => {
    expect(src).toMatch(/openAuthedApiFile/);
    expect(src).toMatch(/DeliverableOpenButton/);
    expect(src).not.toMatch(/href=\{d\.url\}/);
  });

  it("does not promote raw task-detail parents into certain blocking-cause copy", () => {
    expect(src).not.toMatch(/Wartet auf direkte Parents/);
    expect(src).not.toMatch(/Fan-in: .*Parents müssen abgeschlossen sein/);
    expect(src).not.toMatch(/Wartet auf direkte Parents:/);
    expect(src).not.toMatch(/Parent-Warten/);
    expect(src).not.toMatch(/label=\{`Parent /);
  });

  it("caveats chain-start copy as release-only rather than a force-run promise", () => {
    expect(src).toMatch(/keine Scheduler-Zusage/);
    expect(src).toMatch(/Queue\/Assignee/);
    expect(src).toMatch(/gibt gehaltene Subtasks frei/);
  });

  it("guards single dispatch for held Flow subtasks with a chain-first choice", () => {
    expect(src).toMatch(/getHeldFlowDispatchGuard/);
    expect(src).toMatch(/singleDispatch/);
    expect(src).toMatch(/onReleaseChain/);
    expect(src).toMatch(/onDispatchSingle/);
  });

  it("surfaces the routing gate proposal before releasing held Flow chains", () => {
    expect(src).toMatch(/useFlowGate/);
    expect(src).toMatch(/assignee_overrides/);
    expect(src).toMatch(/release_level/);
    expect(src).toMatch(/gate\.sizing/);
    expect(src).toMatch(/sweepTimeouts/);
    expect(src).toMatch(/Soft-Limit/);
    expect(src).toMatch(/Risiko/);
  });

  it("uses scoped Worker action failure copy in the Worker strip", () => {
    expect(src).toMatch(/de\.worker\.actionFailed/);
    expect(src).not.toMatch(/Aktion fehlgeschlagen/);
  });

  it("keeps task deep-links visible and URL-backed", () => {
    expect(src).toMatch(/useSearchParams/);
    expect(src).toMatch(/const taskParam = searchParams\.get\("task"\)/);
    expect(src).toMatch(/setSearchParams\([\s\S]*replace: true/);
    expect(src).toMatch(/flowTaskDomId/);
    expect(src).toMatch(/flowChainDomId/);
    expect(src).toMatch(/scrollToFlowTask/);
    expect(src).toMatch(/id=\{flowTaskDomId\(m\.id\)\}/);
    expect(src).toMatch(/id=\{flowTaskDomId\(id\)\}/);
  });

  it("applies the ?task= deep-link once per value, not on every poll tick", () => {
    // Ohne den One-Shot-Guard lief der Effekt bei jeder neuen Board-Identität
    // (8s-Poll) erneut: Scroll-Yank zur Karte, Re-Expand manuell eingeklappter
    // Ketten, Revert manuell gewählter Projekt-Filter.
    expect(src).toMatch(/handledTaskParam !== taskParam && allTasks\.length > 0/);
    expect(src).toMatch(/setHandledTaskParam\(taskParam\)/);
    expect(src).toMatch(/deepLinkScrolledRef\.current === taskParam\) return/);
  });

  it("anchors relative-time labels to the client clock, not the 304-frozen payload now", () => {
    // Date.now() im Render verletzt react-hooks/purity — die Client-Uhr
    // kommt aus einem externen Store, der Anker-Kontrakt bleibt derselbe.
    expect(src).toMatch(/useClientNowSeconds\(\)/);
    expect(src).toMatch(/Math\.max\(board\.data\?\.now \?\? 0, clientNow\)/);
    expect(src).not.toMatch(/const now = Math\.max\(board\.data\?\.now \?\? 0, Math\.floor\(Date\.now\(\)/);
  });

  it("opens the flow-plan spec through the authenticated opener, not a raw anchor", () => {
    expect(src).toMatch(/DeliverableOpenButton url=\{specUrl\}/);
    expect(src).not.toMatch(/href=\{specUrl\}/);
  });

  it("keeps an active verifier exclusive but restores manual review actions after terminal-verdict grace", () => {
    expect(src).toMatch(/VERIFIER_GATE_TERMINAL_GRACE_MS = 60_000/);
    expect(src).toMatch(/activeVerifier[^\n]+return false/);
    expect(src).toMatch(/manualReviewFallbackById/);
    expect(src).toMatch(/Übergang ausgeblieben — manuell abnehmen/);
  });

  it("derives the terminal-verdict grace from the server verdict stamp, never from render-phase ref state", () => {
    // REQUEST_CHANGES-Befund Run 1018/1021: Ref-Mutation im useMemo verletzt
    // die react-hooks-Purity-Regel ("Cannot update ref during render").
    // Der Zeitanker ist jetzt das submitted_at des Review-Runs — rein
    // ableitbar, kein Client-Zustand, überlebt Reloads.
    expect(src).not.toMatch(/verifierGateFirstSeenRef/);
    expect(src).toMatch(/reviewVerdictAt: r\.active_verifier \? null : r\.submitted_at \?\? null/);
    expect(src).toMatch(/now - verdictAt >= VERIFIER_GATE_TERMINAL_GRACE_MS \/ 1000/);
  });
});

describe("FlowView mobile compaction + scroll stability (Variante B)", () => {
  it("opens the receipt rail as a bottom sheet instead of auto-scrolling to a stacked rail", () => {
    // Der frühere railRef-Scroll schob die Seite bei jedem Tap ans Seitenende.
    expect(src).not.toMatch(/railRef/);
    expect(src).toMatch(/function FlowDetailSheet/);
    expect(src).toMatch(/setDetailSheetOpen\(true\)/);
    // Desktop behält die sticky Seitenleiste; das Sheet bleibt dort versteckt.
    expect(src).toMatch(/hidden xl:block/);
    expect(src).toMatch(/xl:hidden/);
  });

  it("marks self-set ?task= params as handled so the deep-link effect cannot re-scroll per tap", () => {
    expect(src).toMatch(/setHandledTaskParam\(id\);/);
    expect(src).toMatch(/deepLinkScrolledRef\.current = id;/);
  });

  it("pins the auto-expanded chain across poll-tick urgency reorders", () => {
    expect(src).toMatch(/setAutoExpand\(fallbackRoot\)/);
    expect(src).not.toMatch(/autoExpandRef/);
    expect(src).not.toMatch(/expandedRoot \?\? chainBoard\.active\[0\]/);
  });

  it("keeps the worker sort deterministic across payload reorders", () => {
    expect(src).toMatch(/a\.started_at - b\.started_at/);
    expect(src).toMatch(/a\.run_id\.localeCompare\(b\.run_id\)/);
  });

  it("hides the explainer copy on phones but keeps it for sm+", () => {
    expect(src).toMatch(/hidden sm:inline/);
  });

  it("keeps Flow rows bounded on phones instead of clipping off the right edge", () => {
    expect(src).toMatch(/mobileOverflowGuard/);
    expect(src).toMatch(/flex min-w-0 flex-wrap items-center gap-2/);
    expect(src).toMatch(/hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim/);
    expect(src).toMatch(/flex w-full min-w-0 flex-wrap items-center gap-2/);
    expect(src).toMatch(/min-w-0 max-w-full overflow-hidden/);
  });

  it("bounds the receipt rail, chain-insight and plan rows so long ids/branches cannot clip on phones", () => {
    // FlowReceiptRail: run-meta mono line truncates, header row shrinks the status pill.
    expect(src).toMatch(/mt-1 truncate hc-mono hc-type-label hc-dim/);
    expect(src).toMatch(/ml-auto inline-flex shrink-0 items-center gap-1 hc-type-label/);
    // FlowChainInsight: concatenated task lines wrap instead of pushing the right edge.
    expect(src).toMatch(/mt-1 break-words text-\[0\.75rem\] hc-soft/);
    // FlowPlanPanel: subtask id span follows the mono-id truncate pattern.
    expect(src).toMatch(/flex min-w-0 flex-wrap items-center gap-2 rounded-md border/);
  });

  it("keeps the PlanSpec hub open-scoped and readable on phones", () => {
    expect(src).toMatch(/Planspec-Hub/);
    expect(src).toMatch(/offen · Vault/);
    expect(src).toMatch(/aria-expanded=\{plansOpen\}/);
    expect(src).toMatch(/Offene PlanSpecs/);
    expect(src).toMatch(/plansOpen \? <div className="mt-3 grid gap-2">/);
    expect(src).toMatch(/break-words text-sm font-semibold leading-snug text-white/);
    expect(src).toMatch(/mt-1 break-all hc-mono hc-type-label hc-dim sm:line-clamp-1 sm:break-normal/);
    expect(src).toMatch(/min-h-11 items-center gap-1.5 rounded-full/);
  });

  it("keeps the PlanSpec hub at the bottom of the Flow tab", () => {
    expect(src.indexOf("<PlanSpecHub onIngested={onCaptured} />")).toBeGreaterThan(src.indexOf("<DeliveredList"));
    expect(src.indexOf("<PlanSpecHub onIngested={onCaptured} />")).toBeLessThan(src.indexOf("{detailSheetOpen && selectedId ?"));
  });

  it("collapses worker cards to compact headers in the worker strip", () => {
    expect(src).toMatch(/collapsible/);
  });
});
