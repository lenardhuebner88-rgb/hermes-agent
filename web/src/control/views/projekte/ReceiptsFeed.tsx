import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../../components/primitives";
import { SubtabChips, type SubtabItem } from "../../components/leitstand";
import { fmtRelativeTime } from "../../lib/derive";
import type { ProjectReceiptEntry } from "../../lib/schemas";
import { de } from "../../i18n/de";
import { receiptEpoch } from "./derive";
import { ReceiptSheet } from "./ReceiptSheet";

const t = de.projekte;

/** Anzeige-Cap des Feeds (~12 Zeilen); der Rest sitzt hinter "Alle N
 *  anzeigen". Der Backend-Cap (30) bleibt die harte Obergrenze. */
export const RECEIPTS_VISIBLE = 12;

/** Sentinel-ID für den "Ohne Projekt"-Chip (project == null). */
const PROJECT_NONE = "__none__";
const FILTER_ALL = "all";

export interface ReceiptRowProps {
  receipt: ProjectReceiptEntry;
  /** slug → display name for the project chip; missing slugs fall back to the raw slug. */
  projectNames: Readonly<Record<string, string>>;
  now: number;
  /** Projekt-Chip unterdrücken (Projekt-Drawer ist schon slug-scoped). */
  showProject?: boolean;
  onOpen: (receipt: ProjectReceiptEntry) => void;
}

/** Eine Receipt-Zeile — geteilt zwischen Ergebnisse-Feed und Projekt-Drawer
 *  (DRY): Agent-Badge, Titel (truncate), Projekt-Chip (aufgelöster Anzeige-
 *  name, projectNames-Idiom der anderen Sektionen), relatives Alter aus der
 *  ISO-mtime. Die ganze Zeile ist EIN Button (Klick öffnet das Lese-Sheet):
 *  min-h-11 = 44px Touch-Ziel mobil, ab tab die kompakte Dichte (LiveBoard-
 *  Idiom). */
export function ReceiptRow({ receipt, projectNames, now, showProject = true, onOpen }: ReceiptRowProps) {
  const title = receipt.title || receipt.filename;
  const projectName =
    receipt.project == null ? null : (projectNames[receipt.project] ?? receipt.project);

  return (
    <li>
      <button
        type="button"
        aria-label={t.receiptOpenAria(title)}
        title={title}
        onClick={() => onOpen(receipt)}
        className="flex min-h-11 w-full min-w-0 items-center gap-2 rounded-card border border-line-soft bg-surface-2 px-2.5 py-1.5 text-left hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-bronze tab:min-h-0"
      >
        <span className="inline-flex max-w-28 shrink-0 items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 font-data text-micro text-ink-2">
          <span className="truncate">{receipt.agent || "—"}</span>
        </span>
        <p className="min-w-0 flex-1 truncate text-micro text-ink">{title}</p>
        {showProject && projectName ? (
          <span className="inline-flex max-w-full shrink-0 items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 text-micro text-ink-3">
            <span className="truncate">{projectName}</span>
          </span>
        ) : null}
        <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
          {fmtRelativeTime(receiptEpoch(receipt.mtime), now)}
        </span>
      </button>
    </li>
  );
}

export interface ReceiptsFeedProps {
  receipts: ReadonlyArray<ProjectReceiptEntry>;
  /** slug → display name for the project chip; missing slugs fall back to the raw slug. */
  projectNames: Readonly<Record<string, string>>;
  now: number;
  /** Fetch failed: empty list → inline error in the section; data still present → keep list. */
  error?: boolean;
}

/** Count map → chips sorted by count desc, then label alpha. */
function chipsFromCounts(
  counts: Map<string, { label: string; count: number }>,
  allCount: number,
): SubtabItem[] {
  const sorted = [...counts.entries()].sort((a, b) => {
    if (b[1].count !== a[1].count) return b[1].count - a[1].count;
    return a[1].label.localeCompare(b[1].label, "de");
  });
  return [
    { id: FILTER_ALL, label: t.receiptsFilterAll, count: allCount },
    ...sorted.map(([id, { label, count }]) => ({ id, label, count })),
  ];
}

/** "Ergebnisse" — der projektübergreifende Receipt-Feed: welcher Agent hat
 *  zuletzt was abgeschlossen (Vault-Receipts, neueste zuerst, Backend-Cap 30).
 *  Zeilenklick öffnet das Lese-Sheet (ReceiptSheet). Read-only by design —
 *  der Feed beantwortet "was kam raus", nicht "greif ein".
 *  Filterbar per Agent- und Projekt-Chips (UND-Kombination, local state). */
export function ReceiptsFeed({ receipts, projectNames, now, error = false }: ReceiptsFeedProps) {
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<ProjectReceiptEntry | null>(null);
  const [agentFilter, setAgentFilter] = useState<string>(FILTER_ALL);
  const [projectFilter, setProjectFilter] = useState<string>(FILTER_ALL);

  const agentChips = useMemo(() => {
    const counts = new Map<string, { label: string; count: number }>();
    for (const receipt of receipts) {
      const id = receipt.agent;
      const prev = counts.get(id);
      counts.set(id, { label: id, count: (prev?.count ?? 0) + 1 });
    }
    return chipsFromCounts(counts, receipts.length);
  }, [receipts]);

  const projectChips = useMemo(() => {
    const counts = new Map<string, { label: string; count: number }>();
    let noneCount = 0;
    for (const receipt of receipts) {
      if (receipt.project == null) {
        noneCount += 1;
        continue;
      }
      const id = receipt.project;
      const label = projectNames[id] ?? id;
      const prev = counts.get(id);
      counts.set(id, { label, count: (prev?.count ?? 0) + 1 });
    }
    if (noneCount > 0) {
      counts.set(PROJECT_NONE, { label: t.receiptsFilterNoProject, count: noneCount });
    }
    return chipsFromCounts(counts, receipts.length);
  }, [receipts, projectNames]);

  const filtered = useMemo(() => {
    return receipts.filter((receipt) => {
      if (agentFilter !== FILTER_ALL && receipt.agent !== agentFilter) return false;
      if (projectFilter === FILTER_ALL) return true;
      if (projectFilter === PROJECT_NONE) return receipt.project == null;
      return receipt.project === projectFilter;
    });
  }, [receipts, agentFilter, projectFilter]);

  const visible = expanded ? filtered : filtered.slice(0, RECEIPTS_VISIBLE);
  const isFiltered = agentFilter !== FILTER_ALL || projectFilter !== FILTER_ALL;

  return (
    <section aria-label={t.receiptsTitle} className="min-w-0 space-y-3">
      <header>
        <Eyebrow>{t.receiptsEyebrow}</Eyebrow>
        <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.receiptsTitle}</h3>
      </header>

      {/* Fehler ist IMMER sichtbar, sobald er anliegt — auch wenn noch alte
          Daten gerendert werden (der frühere Top-Banner trug dieses Signal;
          seit er inline lebt, darf der Stale-Daten-Fall nicht stumm werden). */}
      {error ? (
        <div className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          {t.receiptsError}
        </div>
      ) : null}

      {receipts.length === 0 ? (
        error ? null : <p className="text-sec text-ink-3">{t.receiptsEmpty}</p>
      ) : (
        <>
          {/* Chip-Zeilen: horizontal scrollbar via SubtabChips (overflow-x-auto),
              Container min-w-0 damit die Seite nicht mit-scrollt (390px mobil). */}
          <div className="min-w-0 space-y-1.5">
            <SubtabChips
              items={agentChips}
              active={agentFilter}
              onSelect={setAgentFilter}
              ariaLabelPrefix="Agent-Filter"
            />
            <SubtabChips
              items={projectChips}
              active={projectFilter}
              onSelect={setProjectFilter}
              ariaLabelPrefix="Projekt-Filter"
            />
          </div>

          {filtered.length === 0 ? (
            <p className="text-sec text-ink-3">
              {isFiltered ? t.receiptsEmptyFiltered : t.receiptsEmpty}
            </p>
          ) : (
            <>
              <ul className="min-w-0 space-y-1">
                {visible.map((receipt) => (
                  <ReceiptRow
                    key={`${receipt.agent}:${receipt.filename}`}
                    receipt={receipt}
                    projectNames={projectNames}
                    now={now}
                    onOpen={setSelected}
                  />
                ))}
              </ul>
              {filtered.length > RECEIPTS_VISIBLE ? (
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => setExpanded((open) => !open)}
                  className="flex min-h-11 w-full items-center gap-2 rounded-card border border-line bg-surface-1 px-3 py-2 text-left text-sec text-ink-2 hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-bronze tab:min-h-0 tab:w-auto"
                >
                  <ChevronRight
                    aria-hidden
                    className={cn(
                      "h-4 w-4 shrink-0 text-ink-3 transition-transform duration-150 ease-out motion-reduce:transition-none",
                      expanded ? "rotate-90" : "",
                    )}
                  />
                  {expanded ? t.receiptsShowLess : t.receiptsShowAll(filtered.length)}
                </button>
              ) : null}
            </>
          )}
        </>
      )}

      {selected ? <ReceiptSheet receipt={selected} onClose={() => setSelected(null)} /> : null}
    </section>
  );
}
