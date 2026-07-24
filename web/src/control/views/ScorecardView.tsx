import { CheckCircle2 } from "lucide-react";
import { KpiTile, ListRow, SectionHeader, StatusChip } from "../components/leitstand";
import { useScorecard } from "../hooks/scorecard";

const percent = (value: number | null) => value == null ? "–" : `${(value * 100).toFixed(1)} %`;

export function ScorecardView() {
  const { data, loading, error } = useScorecard();
  if (loading && !data) return <div className="hc-dim p-6">Scorecard wird geladen …</div>;
  if (error || !data) return <div className="hc-dim p-6">Scorecard ist derzeit nicht verfügbar.</div>;
  return <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
    <header><p className="hc-type-label hc-dim">KANBAN · QUALITÄT</p><h1 className="hc-type-display">Lane Scorecard</h1><p className="hc-dim">Review-Entscheidungen, nach Lane und Modell aufgeschlüsselt.</p></header>
    <section className="grid gap-3 sm:grid-cols-3">
      <KpiTile label="Approval rate" value={percent(data.overall.approval_rate)} delta={`${data.overall.approved} freigegeben`} />
      <KpiTile label="Review runs" value={String(data.overall.runs)} delta="bewertete Läufe" />
      <KpiTile label="Verteilung" value={`${data.verdicts.approved} / ${data.verdicts.rejected}`} delta="approved / rejected" />
    </section>
    <section className="flex flex-col gap-3"><SectionHeader label="Lanes" meta={`${data.profiles.length} Profile`} />
      {data.profiles.map((row) => <ListRow key={row.name} leading={<span className="size-2 rounded-full bg-[var(--hc-data-1)]" aria-hidden />} title={row.name} meta={`${percent(row.approval_rate)} · ${row.runs} Runs`} trailing={<StatusChip icon={CheckCircle2} label="Approval" value={percent(row.approval_rate)} hint={row.approval_rate != null && row.approval_rate >= .8 ? "stabil" : "prüfen"} tone={row.approval_rate != null && row.approval_rate >= .8 ? "emerald" : "amber"} />} />)}
    </section>
    <section className="flex flex-col gap-3"><SectionHeader label="Modelle" meta={`${data.models.length} aktiv`} />
      {data.models.map((row) => <ListRow key={row.name} title={row.name} meta={`${percent(row.approval_rate)} · ${row.runs} Runs`} />)}
    </section>
    <section className="flex flex-col gap-3"><SectionHeader label="Wochentrend" meta="ISO-Wochen" />
      {data.weeks.map((row) => <ListRow key={`${row.year}-${row.week}`} title={`${row.year} · W${String(row.week).padStart(2, "0")}`} meta={`${percent(row.approval_rate)} · ${row.approved}/${row.runs} approved`} />)}
    </section>
  </main>;
}
