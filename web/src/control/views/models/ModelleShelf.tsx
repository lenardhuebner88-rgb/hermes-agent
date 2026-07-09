import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { StatusPill, ToneCallout } from "../../components/atoms";
import {
  DrawerShell,
  FleetEmptyState,
  SectionHeader,
  SubtabChips,
} from "../../components/leitstand";
import { SkeletonCard } from "../../components/primitives";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { extractToc } from "../../lib/slug";
import { toneClasses } from "../../lib/tones";
import { TocNav } from "../knowledge/KnowledgeReader";
import type { ToneName } from "../../lib/types";

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
  providerAngabe: "Provider-Angabe",
  guideLink: "Prompting-Guide →",
  guideBack: "Alle Guides",
  guideLoadError: "Guide konnte nicht geladen werden.",
  guideLoading: "Lade …",
  guidesEmpty: "Noch keine Prompting-Guides.",
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

const MATURITY_TONE: Record<string, ToneName> = {
  curated: "emerald",
  "auto-drafted": "amber",
};

function ScoreChip({ score }: { score: ModelScore }) {
  return (
    <span
      title={`${score.source_name} · ${score.as_of}`}
      className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2 py-0.5 text-[0.68rem] hc-soft"
    >
      <span className="hc-mono text-white">{score.score}{score.unit}</span>
      {humanizeSuite(score.suite)}
      <span className="hc-dim">· {shortDate(score.as_of)}</span>
      {score.claimed_by_provider ? (
        <span className={`rounded-full border px-1 text-[0.58rem] ${toneClasses("amber")}`}>
          {t.providerAngabe}
        </span>
      ) : null}
    </span>
  );
}

function PulseHero({ pulse, updated }: { pulse: ModelPulseItem[]; updated: string }) {
  if (pulse.length === 0 && !updated) return null;
  return (
    <div className="hc-surface-card space-y-1.5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="hc-eyebrow">{t.eyebrow}</p>
        {updated ? <span className="hc-mono text-[0.7rem] hc-dim">{t.stand(updated)}</span> : null}
      </div>
      {pulse.length > 0 ? (
        <div className="flex items-center gap-1.5 overflow-x-auto whitespace-nowrap pb-0.5">
          <span className="shrink-0 text-[0.68rem] hc-dim">{t.pulsePrefix}</span>
          {pulse.map((item, i) => (
            <span
              key={`${item.model}-${i}`}
              className="shrink-0 rounded-full border border-white/10 px-2 py-0.5 text-[0.66rem] hc-soft"
            >
              <span className="hc-mono">{item.model}</span> · {item.date}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ModelCard({ model, onOpenGuide }: { model: LandscapeModel; onOpenGuide: (family: string) => void }) {
  return (
    <div className="hc-surface-card flex h-full flex-col gap-2 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="hc-eyebrow">{model.provider}</p>
          <h4 className="hc-mono truncate text-[0.85rem] font-semibold text-white">{model.id}</h4>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[0.72rem] hc-dim">
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
        <p className="text-[0.72rem] hc-dim">{t.noScores}</p>
      )}
      {model.guide_family ? (
        <button
          type="button"
          onClick={() => onOpenGuide(model.guide_family!)}
          className="mt-auto self-start text-[0.74rem] text-[var(--hc-accent)] hover:underline"
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
    return <FleetEmptyState title={t.noScores} desc="" />;
  }

  return (
    <div className="hc-surface-card overflow-x-auto p-0">
      <table className="w-full min-w-[36rem] border-collapse text-[0.78rem]">
        <thead>
          <tr className="border-b border-[var(--hc-border)] text-left">
            <th className="p-2.5 font-medium hc-dim">{t.modelColumn}</th>
            {suites.map((suite) => (
              <th key={suite} className="p-2.5">
                <button
                  type="button"
                  onClick={() => toggleSort(suite)}
                  className="font-medium hc-dim hover:text-white"
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
            <tr key={model.id} className="border-b border-white/5">
              <td className="hc-mono p-2.5 text-white">{model.id}</td>
              {suites.map((suite) => {
                const score = scoreFor(model, suite);
                return (
                  <td key={suite} className="p-2.5" title={score ? `${score.source_name} · ${score.as_of}` : undefined}>
                    {score ? (
                      <span className="inline-flex items-center gap-1">
                        {score.score}{score.unit}
                        {score.claimed_by_provider ? (
                          <span className={`rounded-full border px-1 text-[0.58rem] ${toneClasses("amber")}`}>
                            {t.providerAngabe}
                          </span>
                        ) : null}
                      </span>
                    ) : (
                      <span className="hc-dim">{t.dash}</span>
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
    return <FleetEmptyState title={t.guidesEmpty} desc="" />;
  }
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {guides.map((guide) => (
        <li key={guide.family}>
          <button
            type="button"
            onClick={() => onOpen(guide.family)}
            className="hc-surface-card flex w-full flex-col gap-1.5 p-3.5 text-left hover:bg-white/5"
          >
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-semibold text-white">{guide.title}</h4>
              {guide.maturity ? (
                <StatusPill tone={MATURITY_TONE[guide.maturity] ?? "zinc"} label={guide.maturity} />
              ) : null}
            </div>
            <p className="hc-mono text-[0.7rem] hc-dim">{guide.family} · {guide.updated}</p>
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
      widthClassName="sm:w-[min(900px,calc(100vw-2rem))]"
    >
      {error ? (
        <ToneCallout tone="red">{t.guideLoadError}<br />{error}</ToneCallout>
      ) : detail ? (
        <div className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)]">
          <aside className="hidden xl:block">
            <div className="hc-surface-card sticky top-4 p-3">
              <p className="mb-2 hc-eyebrow">{t.toc}</p>
              <TocNav entries={toc} onJump={jump} />
            </div>
          </aside>
          <article className="hc-surface-card min-w-0 p-4 sm:p-5">
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
      <SubtabChips items={subtabItems} active={subtab} onSelect={setSubtab} ariaLabelPrefix={t.eyebrow} />

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}

      {data === null && !error ? (
        <SkeletonCard rows={4} />
      ) : data && data.models.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : data ? (
        <>
          {subtab === "uebersicht" ? (
            <div className="space-y-4">
              <PulseHero pulse={data.pulse} updated={data.updated} />
              <SubtabChips items={providerTabs} active={provider ?? ALL_TAB} onSelect={(next) => setProvider(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.providerFilterLabel} />
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
              <SubtabChips items={providerTabs} active={provider ?? ALL_TAB} onSelect={(next) => setProvider(next === ALL_TAB ? null : next)} ariaLabelPrefix={t.providerFilterLabel} />
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
