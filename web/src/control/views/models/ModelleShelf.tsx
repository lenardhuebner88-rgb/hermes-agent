import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  DrawerShell,
  FleetEmptyState,
  SectionHeader,
  SignalChip,
  SignalLabel,
  SubtabChips,
  signalToneFromLegacy,
} from "../../components/leitstand";
import { Eyebrow, SkeletonCard } from "../../components/primitives";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { extractToc } from "../../lib/slug";
import { TocNav } from "../knowledge/KnowledgeReader";

// Modelle: Landscape (model-landscape.md) + gequellte Benchmarks
// (benchmarks.json) + Prompting-Guides (wiki/prompting/*.md) — Backend:
// hermes_cli/library_models.py (`/api/library/models{,/guide}`). Gleiches
// Fetch/Polling-Muster wie ErgebnisseShelf (60s, document.hidden-gated),
// eigener Subtab-/Drawer-Satz.
const t = {
  eyebrow: "Modelle",
  subtabUebersicht: "Übersicht",
  subtabBenchmarks: "Benchmarks",
  subtabPrompting: "Prompting",
  allProviders: "Alle Provider",
  providerFilterLabel: "Provider",
  loadError: "Modelle konnten nicht geladen werden.",
  emptyTitle: "Keine Modelle",
  emptyDesc: "Die Landscape-Datei enthält aktuell keine Modelle.",
  pulsePrefix: "Neu entdeckt:",
  stand: (s: string) => `Stand ${s}`,
  noScores: "Noch keine gequellten Benchmarks.",
  noScoresDesc: "Die Modelle sind noch nicht belastbar vergleichbar.",
  providerAngabe: "Provider-Angabe",
  guideLink: "Prompting-Guide →",
  guideBack: "Alle Guides",
  guideLoadError: "Guide konnte nicht geladen werden.",
  guideLoading: "Lade …",
  guidesEmpty: "Noch keine Prompting-Guides.",
  guidesEmptyDesc: "Für die gelisteten Modelle liegt noch keine Anleitung vor.",
  toc: "Inhalt",
  modelColumn: "Modell",
  dash: "—",
};

const ALL_TAB = "__all";

export interface ModelScore {
  suite: string;
  score: number;
  unit: string;
  source: string;
  as_of: string;
  claimed_by_provider: boolean;
  source_name: string;
  source_url: string;
}

export interface LandscapeModel {
  id: string;
  provider: string;
  family: string;
  context: string;
  price_in: number | null;
  price_out: number | null;
  created: string | null;
  scores: ModelScore[];
  guide_family: string | null;
}

export interface ModelPulseItem {
  date: string;
  model: string;
  detail: string;
}

export interface GuideSummary {
  family: string;
  updated: string;
  maturity: string;
  title: string;
}

export interface ModelsResponse {
  updated: string;
  models: LandscapeModel[];
  pulse: ModelPulseItem[];
  guides: GuideSummary[];
}

export interface GuideDetail {
  family: string;
  frontmatter: Record<string, unknown>;
  body_md: string;
}

type Subtab = "uebersicht" | "benchmarks" | "prompting";

function priceLabel(price: number | null): string {
  return price != null ? `$${price.toFixed(2)}` : "–";
}

function shortDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}.${m[2]}.` : iso;
}

const SUITE_WORD_OVERRIDES: Record<string, string> = { swe: "SWE" };

function humanizeSuite(suite: string): string {
  return suite
    .split("-")
    .map((word) => SUITE_WORD_OVERRIDES[word] ?? word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

const MATURITY_TONE: Record<string, string> = {
  curated: "emerald",
  "auto-drafted": "amber",
};

function ScoreChip({ score }: { score: ModelScore }) {
  return (
    <span
      title={`${score.source_name} · ${score.as_of}`}
      className="inline-flex items-center gap-1 rounded-card border border-line px-2 py-0.5 text-micro text-ink-2"
    >
      <span className="font-data tabular-nums text-ink">{score.score}{score.unit}</span>
      {humanizeSuite(score.suite)}
      <span className="text-ink-3">· {shortDate(score.as_of)}</span>
      {score.claimed_by_provider ? (
        <SignalLabel tone="warn" label={t.providerAngabe} />
      ) : null}
    </span>
  );
}

function PulseHero({ pulse, updated }: { pulse: ModelPulseItem[]; updated: string }) {
  if (pulse.length === 0 && !updated) return null;
  return (
    <div className="space-y-1.5 rounded-card border border-line bg-surface-1 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Eyebrow>{t.eyebrow}</Eyebrow>
        {updated ? <span className="font-data text-micro tabular-nums text-ink-3">{t.stand(updated)}</span> : null}
      </div>
      {pulse.length > 0 ? (
        <div className="flex items-center gap-1.5 overflow-x-auto whitespace-nowrap pb-0.5">
          <span className="shrink-0 text-micro text-ink-3">{t.pulsePrefix}</span>
          {pulse.map((item, i) => (
            <span
              key={`${item.model}-${i}`}
              className="shrink-0 rounded-card border border-line px-2 py-0.5 text-micro text-ink-2"
            >
              <span className="font-data">{item.model}</span> · {item.date}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ModelCard({ model, onOpenGuide }: { model: LandscapeModel; onOpenGuide: (family: string) => void }) {
  return (
    <div className="flex h-full flex-col gap-2 rounded-card border border-line bg-surface-2 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Eyebrow>{model.provider}</Eyebrow>
          <h4 className="truncate text-body font-semibold text-ink">{model.id}</h4>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-micro text-ink-3">
        <span>{model.context}</span>
        <span>{priceLabel(model.price_in)} / {priceLabel(model.price_out)} · 1M</span>
        {model.created ? <span>{model.created}</span> : null}
      </div>
      {model.scores.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {model.scores.slice(0, 3).map((score, i) => (
            <ScoreChip key={`${score.suite}-${i}`} score={score} />
          ))}
        </div>
      ) : (
        <p className="text-micro text-ink-3">{t.noScores}</p>
      )}
      {model.guide_family ? (
        <button
          type="button"
          onClick={() => onOpenGuide(model.guide_family!)}
          className="mt-auto inline-flex min-h-12 items-center self-start rounded-card text-sec text-bronze-hi hover:underline"
        >
          {t.guideLink}
        </button>
      ) : null}
    </div>
  );
}

function BenchmarksTable({ models }: { models: LandscapeModel[] }) {
  const suites = useMemo(() => {
    const seen = new Set<string>();
    for (const m of models) for (const s of m.scores) seen.add(s.suite);
    return Array.from(seen).sort();
  }, [models]);

  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const scoreFor = useCallback(
    (model: LandscapeModel, suite: string) => model.scores.find((s) => s.suite === suite) ?? null,
    [],
  );

  const sorted = useMemo(() => {
    if (!sortKey) return models;
    const withScore = models.filter((m) => scoreFor(m, sortKey) != null);
    const without = models.filter((m) => scoreFor(m, sortKey) == null);
    withScore.sort((a, b) => {
      const av = scoreFor(a, sortKey)!.score;
      const bv = scoreFor(b, sortKey)!.score;
      return sortDir === "asc" ? av - bv : bv - av;
    });
    return [...withScore, ...without];
  }, [models, sortKey, sortDir, scoreFor]);

  const toggleSort = (suite: string) => {
    if (sortKey === suite) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(suite);
      setSortDir("desc");
    }
  };

  if (suites.length === 0) {
    return <FleetEmptyState title={t.noScores} desc={t.noScoresDesc} />;
  }

  return (
    <div className="overflow-x-auto rounded-panel border border-line bg-surface-1">
      <table className="w-full min-w-[36rem] border-collapse text-sec">
        <thead>
          <tr className="border-b border-line-soft text-left">
            <th className="p-2.5 font-display text-micro font-medium uppercase tracking-[0.08em] text-ink-3">{t.modelColumn}</th>
            {suites.map((suite) => (
              <th key={suite} className="p-2.5">
                <button
                  type="button"
                  onClick={() => toggleSort(suite)}
                  className="min-h-12 font-display text-micro font-medium uppercase tracking-[0.08em] text-ink-3 hover:text-ink"
                >
                  {humanizeSuite(suite)}
                  {sortKey === suite ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((model) => (
            <tr key={model.id} className="border-b border-line-soft">
              <td className="p-2.5 font-data text-ink">{model.id}</td>
              {suites.map((suite) => {
                const score = scoreFor(model, suite);
                return (
                  <td key={suite} className="p-2.5" title={score ? `${score.source_name} · ${score.as_of}` : undefined}>
                    {score ? (
                      <span className="inline-flex items-center gap-1">
                        {score.score}{score.unit}
                        {score.claimed_by_provider ? (
                          <SignalLabel tone="warn" label={t.providerAngabe} />
                        ) : null}
                      </span>
                    ) : (
                      <span className="text-ink-3">{t.dash}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GuideList({ guides, onOpen }: { guides: GuideSummary[]; onOpen: (family: string) => void }) {
  if (guides.length === 0) {
    return <FleetEmptyState title={t.guidesEmpty} desc={t.guidesEmptyDesc} />;
  }
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {guides.map((guide) => (
        <li key={guide.family}>
          <button
            type="button"
            onClick={() => onOpen(guide.family)}
            className="flex min-h-12 w-full flex-col gap-1.5 rounded-card border border-line bg-surface-2 p-3.5 text-left hover:bg-surface-3"
          >
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sec font-semibold text-ink">{guide.title}</h4>
              {guide.maturity ? (
                <SignalChip tone={signalToneFromLegacy(MATURITY_TONE[guide.maturity])} label={guide.maturity} />
              ) : null}
            </div>
            <p className="font-data text-micro text-ink-3">{guide.family} · {guide.updated}</p>
          </button>
        </li>
      ))}
    </ul>
  );
}

function GuideDrawer({ family, onClose }: { family: string; onClose: () => void }) {
  const [detail, setDetail] = useState<GuideDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const d = await fetchJSON<GuideDetail>(`/api/library/models/guide?family=${encodeURIComponent(family)}`);
        if (!cancelled) setDetail(d);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [family]);

  const toc = useMemo(() => (detail ? extractToc(detail.body_md) : []), [detail]);
  const jump = (slug: string) => {
    const el = typeof document !== "undefined" ? document.getElementById(slug) : null;
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <DrawerShell
      eyebrow={t.subtabPrompting}
      title={(detail?.frontmatter.title as string | undefined) ?? family}
      ariaLabel={`${t.subtabPrompting}: ${family}`}
      closeLabel={t.guideBack}
      onClose={onClose}
      widthClassName="tab:w-[min(900px,calc(100vw-2rem))]"
    >
      {error ? (
        <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={t.guideLoadError} /><p className="mt-1 text-sec text-ink-2">{error}</p></div>
      ) : detail ? (
        <div className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)]">
          <aside className="hidden xl:block">
            <div className="sticky top-4 rounded-card border border-line bg-surface-2 p-3">
              <Eyebrow className="mb-2">{t.toc}</Eyebrow>
              <TocNav entries={toc} onJump={jump} />
            </div>
          </aside>
          <article className="min-w-0 rounded-card border border-line bg-surface-2 p-4 sm:p-5">
            <ProseMarkdown slugHeadings wrapTables>{detail.body_md}</ProseMarkdown>
          </article>
        </div>
      ) : (
        <SkeletonCard rows={6} />
      )}
    </DrawerShell>
  );
}

export function ModelleShelf() {
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [subtab, setSubtab] = useState<Subtab>("uebersicht");
  const [provider, setProvider] = useState<string | null>(null);
  const [openGuideFamily, setOpenGuideFamily] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetchJSON<ModelsResponse>("/api/library/models");
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(handle);
  }, [load]);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 60000);
    return () => window.clearInterval(id);
  }, [load]);

  const openGuide = useCallback((family: string) => {
    setOpenGuideFamily(family);
    setSubtab("prompting");
  }, []);

  const providerTabs = useMemo(() => {
    const providers = Array.from(new Set((data?.models ?? []).map((m) => m.provider))).sort();
    return [{ id: ALL_TAB, label: t.allProviders }, ...providers.map((p) => ({ id: p, label: p }))];
  }, [data]);

  const filteredModels = useMemo(() => {
    const models = data?.models ?? [];
    return provider ? models.filter((m) => m.provider === provider) : models;
  }, [data, provider]);

  const subtabItems = [
    { id: "uebersicht", label: t.subtabUebersicht },
    { id: "benchmarks", label: t.subtabBenchmarks },
    { id: "prompting", label: t.subtabPrompting },
  ] as const;

  return (
    <div className="space-y-4">
      <SubtabChips items={subtabItems} active={subtab} onSelect={setSubtab} ariaLabelPrefix={t.eyebrow} className="[&_button]:min-h-12" />

      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={t.loadError} /><p className="mt-1 text-sec text-ink-2">{error}</p></div> : null}

      {data === null && !error ? (
        <SkeletonCard rows={4} />
      ) : data && data.models.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : data ? (
        <>
          {subtab === "uebersicht" ? (
            <div className="space-y-4">
              <PulseHero pulse={data.pulse} updated={data.updated} />
              <SubtabChips items={providerTabs} active={provider ?? ALL_TAB} onSelect={(next) => setProvider(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.providerFilterLabel} className="[&_button]:min-h-12" />
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {filteredModels.map((model) => (
                  <ModelCard key={model.id} model={model} onOpenGuide={openGuide} />
                ))}
              </div>
            </div>
          ) : null}

          {subtab === "benchmarks" ? (
            <div className="space-y-4">
              <SectionHeader label={t.subtabBenchmarks} meta={t.stand(data.updated)} />
              <SubtabChips items={providerTabs} active={provider ?? ALL_TAB} onSelect={(next) => setProvider(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.providerFilterLabel} className="[&_button]:min-h-12" />
              <BenchmarksTable models={filteredModels} />
            </div>
          ) : null}

          {subtab === "prompting" ? (
            <div className="space-y-4">
              <SectionHeader label={t.subtabPrompting} />
              <GuideList guides={data.guides} onOpen={openGuide} />
            </div>
          ) : null}
        </>
      ) : null}

      {openGuideFamily ? (
        <GuideDrawer
          key={openGuideFamily}
          family={openGuideFamily}
          onClose={() => setOpenGuideFamily(null)}
        />
      ) : null}
    </div>
  );
}
