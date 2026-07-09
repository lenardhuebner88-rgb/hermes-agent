import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { StatusPill, ToneCallout } from "../../components/atoms";
import {
  DrawerShell,
  FleetEmptyState,
  ListRow,
  SectionHeader,
  SubtabChips,
} from "../../components/leitstand";
import { SkeletonCard } from "../../components/primitives";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { fmtClock } from "../../lib/derive";
import { dedupeById } from "../BibliothekView.helpers";
import { CopyButton } from "../backlog/CopyButton";
import type { ToneName } from "../../lib/types";

// Ergebnisse: abgeschlossene Kanban-Tasks (task_runs-Verdict/Outcome/Cost) als
// Mensch- UND LLM-lesbares Digest — Backend: hermes_cli/library_results.py
// (`/api/library/results{,/item}`). Gleiches Fetch/Polling/"Mehr laden"-
// Muster wie LesesaalBody (BibliothekView.tsx), eigener Filter-/Drawer-Satz.
const t = {
  eyebrow: "Ergebnisse",
  searchPlaceholder: "Suche in Titel + Ergebnis …",
  allKinds: "Alle Arten",
  allProfiles: "Alle Profile",
  allVerdicts: "Alle Verdicts",
  kindFilterLabel: "Art",
  profileFilterLabel: "Profil",
  verdictFilterLabel: "Verdict",
  loadError: "Ergebnisse konnten nicht geladen werden.",
  emptyTitle: "Noch keine Ergebnisse",
  emptyDesc: "Sobald Tasks abgeschlossen sind, erscheinen ihre Ergebnisse hier.",
  loadMore: "Mehr laden",
  loadingMore: "Lade …",
  issues: (n: number) => `${n} Ergebnis${n === 1 ? "" : "se"}`,
  back: "← Übersicht",
  runsTitle: "Runs",
  artifactsTitle: "Artefakte",
  copyForAgent: "Copy für Agent",
  copied: "Kopiert",
  noArtifacts: "Keine Artefakte.",
};

const RESULTS_PAGE_SIZE = 20;
const ALL_TAB = "__all";

export interface ResultItem {
  id: string;
  title: string;
  kind: string | null;
  profile: string | null;
  completed_at: string | null;
  result_summary: string;
  verdict: string | null;
  outcome: string | null;
  cost_usd: number | null;
  run_count: number;
}

interface ResultRun {
  started: string | null;
  outcome: string | null;
  verdict: string | null;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  summary: string | null;
}

interface ResultArtifact {
  title: string;
  id: string;
  category: string;
}

interface ResultItemDetail extends ResultItem {
  result_md: string;
  runs: ResultRun[];
  artifacts: ResultArtifact[];
}

interface ResultListResponse {
  items: ResultItem[];
  total: number;
}

// Real outcome/verdict enum values (harvested from the live task_runs schema,
// 2026-07-09 — see test_library_results.py fixture) mapped to --hc-* tones.
// Unknown/unseen values fall back to a neutral chip, never invented labels.
const VERDICT_TONE: Record<string, ToneName> = {
  APPROVED: "emerald",
  REQUEST_CHANGES: "red",
};

const OUTCOME_TONE: Record<string, ToneName> = {
  completed: "emerald",
  blocked: "red",
  crashed: "red",
  gave_up: "red",
  spawn_failed: "red",
  timed_out: "amber",
  reclaimed: "amber",
  scheduled: "cyan",
  iteration_budget_exhausted: "amber",
  integration_parked: "amber",
  deliverable_posted_not_completed: "amber",
  transient_retry: "amber",
};

function toEpoch(iso: string | null): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : 0;
}

function costLabel(cost: number | null): string {
  return cost != null ? `$${cost.toFixed(4)}` : "–";
}

function groupByDay(items: ResultItem[]): { day: string; items: ResultItem[] }[] {
  const groups: { day: string; items: ResultItem[] }[] = [];
  const index = new Map<string, ResultItem[]>();
  for (const item of items) {
    const day = item.completed_at
      ? new Date(toEpoch(item.completed_at) * 1000).toLocaleDateString("de-DE", {
          day: "2-digit", month: "2-digit", year: "numeric",
        })
      : "–";
    let bucket = index.get(day);
    if (!bucket) {
      bucket = [];
      index.set(day, bucket);
      groups.push({ day, items: bucket });
    }
    bucket.push(item);
  }
  return groups;
}

// Mirrors the backend's `format=md` per-item block (library_results.py,
// `_render_md_digest`) so "Copy für Agent" produces the identical structure
// the LLM digest endpoint would — composed client-side from the already-
// fetched detail instead of a second round-trip.
function composeAgentCopyText(detail: ResultItemDetail): string {
  const metaLine = [
    detail.kind ?? "-",
    detail.profile ?? "-",
    detail.completed_at ?? "-",
    detail.verdict ?? "-",
    costLabel(detail.cost_usd),
  ].join(" · ");
  let block = `## ${detail.title} — ${detail.id}\n${metaLine}\n\n${detail.result_md}`;
  if (detail.artifacts.length > 0) {
    const links = detail.artifacts.map((a) => `- [${a.title}](${a.id})`).join("\n");
    block += `\n\n${t.artifactsTitle}:\n${links}`;
  }
  return block;
}

function ResultBadges({ item }: { item: ResultItem }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {item.kind ? <span className="hc-eyebrow">{item.kind}</span> : null}
      {item.profile ? (
        <span className="rounded-full border border-white/10 px-2 py-0.5 text-[0.65rem] hc-soft">{item.profile}</span>
      ) : null}
      {item.verdict ? <StatusPill tone={VERDICT_TONE[item.verdict] ?? "zinc"} label={item.verdict} /> : null}
      {item.outcome ? <StatusPill tone={OUTCOME_TONE[item.outcome] ?? "zinc"} label={item.outcome} /> : null}
    </div>
  );
}

function ResultRowItem({ item, onOpen }: { item: ResultItem; onOpen: (item: ResultItem) => void }) {
  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen(item);
    }
  };
  return (
    <li>
      <div role="button" tabIndex={0} onClick={() => onOpen(item)} onKeyDown={handleKeyDown} className="cursor-pointer">
        <ListRow
          leading={<ResultBadges item={item} />}
          title={item.title}
          meta={item.completed_at ? fmtClock(toEpoch(item.completed_at)) : undefined}
          trailing={<span className="hc-mono text-xs hc-dim">{costLabel(item.cost_usd)}</span>}
          className="hover:bg-white/5"
        >
          <span className="line-clamp-3 text-sm hc-soft">{item.result_summary}</span>
        </ListRow>
      </div>
    </li>
  );
}

function RunsTable({ runs }: { runs: ResultRun[] }) {
  return (
    <div className="space-y-1.5">
      {runs.map((run, idx) => (
        <div key={`${run.started ?? "run"}-${idx}`} className="rounded-lg border border-[var(--hc-border)] bg-black/20 p-2.5">
          <div className="flex flex-wrap items-center gap-2 text-[0.72rem]">
            <span className="hc-mono hc-dim">{run.started ? fmtClock(toEpoch(run.started)) : "–"}</span>
            {run.outcome ? <StatusPill tone={OUTCOME_TONE[run.outcome] ?? "zinc"} label={run.outcome} /> : null}
            {run.verdict ? <StatusPill tone={VERDICT_TONE[run.verdict] ?? "zinc"} label={run.verdict} /> : null}
            <span className="ml-auto hc-mono hc-dim">{costLabel(run.cost_usd)}</span>
          </div>
          {run.summary ? <p className="mt-1.5 text-sm hc-soft">{run.summary}</p> : null}
        </div>
      ))}
    </div>
  );
}

function DetailBody({
  detail,
  onOpenArtifact,
}: {
  detail: ResultItemDetail;
  onOpenArtifact: (id: string) => void;
}) {
  return (
    <div className="space-y-4">
      <ResultBadges item={detail} />
      <ProseMarkdown>{detail.result_md}</ProseMarkdown>
      <SectionHeader label={t.runsTitle} meta={t.issues(detail.runs.length)} />
      <RunsTable runs={detail.runs} />
      <SectionHeader label={t.artifactsTitle} />
      {detail.artifacts.length === 0 ? (
        <p className="text-sm hc-dim">{t.noArtifacts}</p>
      ) : (
        <ul className="space-y-1">
          {detail.artifacts.map((artifact) => (
            <li key={artifact.id}>
              <button
                type="button"
                onClick={() => onOpenArtifact(artifact.id)}
                className="text-left text-sm text-[var(--hc-accent)] hover:underline"
              >
                {artifact.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ErgebnisseShelf({ onOpenLesesaalItem }: { onOpenLesesaalItem: (id: string) => void }) {
  const [items, setItems] = useState<ResultItem[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string | null>(null);
  const [profile, setProfile] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<string | null>(null);

  const [reading, setReading] = useState<ResultItemDetail | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [readingError, setReadingError] = useState<string | null>(null);

  // Chip-Kandidaten: einmal gesehene kind/profile/verdict-Werte bleiben
  // wählbar, auch wenn die aktuell gefilterte Seite sie gerade nicht enthält
  // (das Backend liefert keinen separaten Enum-Katalog — S1-Anti-Scope).
  const [seenKinds, setSeenKinds] = useState<string[]>([]);
  const [seenProfiles, setSeenProfiles] = useState<string[]>([]);
  const [seenVerdicts, setSeenVerdicts] = useState<string[]>([]);

  const fetchPage = useCallback(async (offset: number) => {
    const params = new URLSearchParams();
    params.set("limit", String(RESULTS_PAGE_SIZE));
    params.set("offset", String(offset));
    if (kind) params.set("kind", kind);
    if (profile) params.set("profile", profile);
    if (verdict) params.set("verdict", verdict);
    if (q.trim()) params.set("q", q.trim());
    return fetchJSON<ResultListResponse>(`/api/library/results?${params.toString()}`);
  }, [kind, profile, verdict, q]);

  const rememberSeen = useCallback((page: ResultItem[]) => {
    const kinds = new Set<string>();
    const profiles = new Set<string>();
    const verdicts = new Set<string>();
    for (const item of page) {
      if (item.kind) kinds.add(item.kind);
      if (item.profile) profiles.add(item.profile);
      if (item.verdict) verdicts.add(item.verdict);
    }
    if (kinds.size) setSeenKinds((cur) => Array.from(new Set([...cur, ...kinds])).sort());
    if (profiles.size) setSeenProfiles((cur) => Array.from(new Set([...cur, ...profiles])).sort());
    if (verdicts.size) setSeenVerdicts((cur) => Array.from(new Set([...cur, ...verdicts])).sort());
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await fetchPage(0);
      setItems(res.items ?? []);
      setTotal(res.total ?? 0);
      rememberSeen(res.items ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [fetchPage, rememberSeen]);

  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const res = await fetchPage(items.length);
      setItems((current) => dedupeById([...current, ...(res.items ?? [])]));
      setTotal(res.total ?? 0);
      rememberSeen(res.items ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, items.length, rememberSeen]);

  // Erst-Load sofort, Filter-/Sucheänderungen entprellt (250ms bei aktivem
  // Suchtext) — Hauskonvention (KnowledgeShelf/LesesaalBody).
  useEffect(() => {
    const handle = window.setTimeout(() => void load(), q.trim() ? 250 : 0);
    return () => window.clearTimeout(handle);
  }, [load, q]);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 60000);
    return () => window.clearInterval(id);
  }, [load]);

  const openDetail = useCallback(async (item: ResultItem) => {
    setDrawerOpen(true);
    setReading(null);
    setReadingError(null);
    try {
      const detail = await fetchJSON<ResultItemDetail>(`/api/library/results/item?id=${encodeURIComponent(item.id)}`);
      setReading(detail);
    } catch (e) {
      setReadingError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const closeDetail = useCallback(() => {
    setDrawerOpen(false);
    setReading(null);
    setReadingError(null);
  }, []);

  const kindTabs = useMemo(
    () => [{ id: ALL_TAB, label: t.allKinds }, ...seenKinds.map((k) => ({ id: k, label: k }))],
    [seenKinds],
  );
  const profileTabs = useMemo(
    () => [{ id: ALL_TAB, label: t.allProfiles }, ...seenProfiles.map((p) => ({ id: p, label: p }))],
    [seenProfiles],
  );
  const verdictTabs = useMemo(
    () => [{ id: ALL_TAB, label: t.allVerdicts }, ...seenVerdicts.map((v) => ({ id: v, label: v }))],
    [seenVerdicts],
  );

  const dayGroups = useMemo(() => groupByDay(items), [items]);
  const hasMore = total != null && items.length < total;

  return (
    <div className="space-y-4">
      <div className="hc-surface-card space-y-2.5 p-3">
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t.searchPlaceholder}
          aria-label={t.searchPlaceholder}
          className="w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-3 py-1.5 text-sm text-white placeholder:hc-dim"
        />
        <SubtabChips items={kindTabs} active={kind ?? ALL_TAB} onSelect={(next) => setKind(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.kindFilterLabel} />
        <SubtabChips items={profileTabs} active={profile ?? ALL_TAB} onSelect={(next) => setProfile(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.profileFilterLabel} />
        <SubtabChips items={verdictTabs} active={verdict ?? ALL_TAB} onSelect={(next) => setVerdict(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.verdictFilterLabel} />
      </div>

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}

      {total === null && !error ? (
        <SkeletonCard rows={4} />
      ) : items.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        <div className="space-y-4">
          {dayGroups.map((group) => (
            <div key={group.day}>
              <SectionHeader label={group.day} meta={t.issues(group.items.length)} />
              <ul className="mt-2 space-y-1.5">
                {group.items.map((item) => (
                  <ResultRowItem key={item.id} item={item} onOpen={(next) => void openDetail(next)} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {hasMore ? (
        <div className="flex justify-center pt-1">
          <button
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            className="inline-flex min-h-9 items-center rounded-full border border-white/10 px-4 py-1.5 text-[0.78rem] hc-soft hover:bg-white/5 disabled:opacity-50"
          >
            {loadingMore ? t.loadingMore : t.loadMore}
          </button>
        </div>
      ) : null}

      {drawerOpen ? (
        <DrawerShell
          eyebrow={t.eyebrow}
          title={reading?.title ?? "…"}
          ariaLabel={`${t.eyebrow}: ${reading?.title ?? ""}`}
          closeLabel={t.back}
          onClose={closeDetail}
          widthClassName="sm:w-[min(900px,calc(100vw-2rem))]"
          footer={reading ? <CopyButton text={composeAgentCopyText(reading)} label={t.copyForAgent} copiedLabel={t.copied} /> : undefined}
        >
          {readingError ? (
            <ToneCallout tone="red">{t.loadError}<br />{readingError}</ToneCallout>
          ) : reading ? (
            <DetailBody detail={reading} onOpenArtifact={onOpenLesesaalItem} />
          ) : (
            <SkeletonCard rows={6} />
          )}
        </DrawerShell>
      ) : null}
    </div>
  );
}
