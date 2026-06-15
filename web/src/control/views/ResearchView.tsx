import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../components/Hero";
import { ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { ModelPicker } from "../components/ModelPicker";
import { ProseMarkdown } from "../components/ProseMarkdown";
import { useHermesWorkers } from "../hooks/useControlData";
import { fmtClock, fmtDur, nowSec } from "../lib/derive";
import { taskStatusLabel } from "../lib/tones";
import type { Density } from "../hooks/useDensity";
import { buildResearchIdempotencyKey, pickAnswer, type ResearchDetail } from "./ResearchView.helpers";

// Phase C (Programm 3): Recherche-Tab — der Operator beauftragt Wissen wie
// einen Worker: Frage → Modellwahl → Task (tenant=research) → Antwort
// gerendert im Tab. Kein Umbau des Autoresearch-Tabs (der bleibt die
// Skill-Selbstverbesserungs-Maschine). Strings lokal (F3/F6-Muster).
const t = {
  eyebrow: "Recherche",
  title: "Wissen beauftragen",
  subtitle: "Frage stellen, Modell wählen — ein Research-Worker antwortet hier und in der Bibliothek.",
  questionLabel: "Frage",
  questionPlaceholder: "Was soll recherchiert werden?",
  depthLabel: "Tiefe",
  depthShort: "kurz",
  depthDeep: "gründlich",
  modelLabel: "Modell (leer = Profil-Default)",
  submit: "Recherche starten",
  submitting: "Wird angelegt …",
  submitted: (id: string) => `Task ${id} läuft — Antwort erscheint unten.`,
  history: "Verlauf",
  historyHint: "Alle Recherchen (tenant=research) · neueste zuerst",
  empty: "Noch keine Recherchen.",
  emptyDesc: "Die erste Frage oben stellt sich in ~1 min als laufender Worker dar.",
  doingNow: "macht gerade",
  etaLine: (p50: string, run: string) => `üblich ~${p50} · läuft ${run}`,
  noAnswer: "Noch keine Antwort — der Worker läuft oder wartet auf Dispatch.",
  answerMeta: (author: string, at: string) => `${author} · ${at}`,
  question: "Frage",
  loadError: "Verlauf konnte nicht geladen werden.",
};

const DEPTH_HINT: Record<string, string> = {
  kurz: "Tiefe: kurz — kompakte Antwort (wenige Absätze), keine Nebenpfade.",
  gründlich: "Tiefe: gründlich — mehrere Quellen, Gegencheck, strukturierter Bericht.",
};

export interface ResearchCard {
  id: string;
  title: string;
  status: string;
  created_at: number;
  latest_summary: string | null;
}

interface BoardLike {
  columns: { name: string; tasks: ResearchCard[] }[];
}



export function ResearchEntry({ card, now }: { card: ResearchCard; now: number }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ResearchDetail | null>(null);
  const workers = useHermesWorkers();
  const worker = (workers.data?.workers ?? []).find((w) => w.task_id === card.id) ?? null;
  const running = card.status === "running";
  const done = card.status === "done";

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async () => {
      try {
        const d = await fetchJSON<ResearchDetail>(`/api/plugins/kanban/tasks/${encodeURIComponent(card.id)}`);
        if (!cancelled) setDetail(d);
      } catch {
        /* Karte zeigt dann den Verlaufs-Stand */
      }
    };
    void load();
    // Solange offen + nicht fertig: Antwort nachpollen.
    const id = done ? 0 : window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 8000);
    return () => { cancelled = true; if (id) window.clearInterval(id); };
  }, [open, card.id, done]);

  const answer = detail ? pickAnswer(detail) : null;
  return (
    <li className="rounded-md border border-[var(--hc-border)] px-3 py-2.5">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="flex w-full flex-wrap items-center gap-2 text-left">
        <span className="min-w-0 flex-1 basis-64 truncate text-[0.88rem] font-medium text-white">{card.title}</span>
        <span className={`rounded-full border px-2 py-0.5 text-[0.7rem] ${done ? "border-emerald-500/40 text-emerald-300" : running ? "border-cyan-500/40 text-cyan-300" : "border-white/10 hc-soft"}`}>
          {taskStatusLabel[card.status as keyof typeof taskStatusLabel] ?? card.status}
        </span>
        <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">{fmtClock(card.created_at)}</span>
      </button>
      {/* Phase-A-Fortschritt erbt die Karte gratis: Note + ehrliche ETA. */}
      {worker ? (
        <p className="mt-1.5 text-[0.78rem] hc-soft">
          {worker.last_heartbeat_note ? <><span className="hc-eyebrow mr-1">{t.doingNow}:</span>{worker.last_heartbeat_note}</> : null}
          {worker.eta_p50_seconds ? (
            <span className="hc-dim"> · {t.etaLine(fmtDur(worker.eta_p50_seconds), fmtDur(Math.max(0, now - worker.started_at)))}</span>
          ) : null}
        </p>
      ) : null}
      {open ? (
        <div className="mt-2 space-y-3 border-t border-[var(--hc-border)] pt-2">
          {detail?.task?.body ? (
            <details>
              <summary className="cursor-pointer text-[0.78rem] hc-dim">{t.question}</summary>
              <p className="mt-1 whitespace-pre-wrap text-[0.82rem] hc-soft">{detail.task.body}</p>
            </details>
          ) : null}
          {answer ? (
            <div>
              {answer.author ? (
                <p className="mb-1 hc-type-label hc-dim">{t.answerMeta(answer.author, answer.at ? fmtClock(answer.at) : "")}</p>
              ) : null}
              <ProseMarkdown>{answer.body}</ProseMarkdown>
            </div>
          ) : (
            <p className="text-[0.82rem] hc-dim">{t.noAnswer}</p>
          )}
        </div>
      ) : null}
    </li>
  );
}

export function ResearchView(_props: { density?: Density }) {
  const [question, setQuestion] = useState("");
  const [model, setModel] = useState("");
  const [depth, setDepth] = useState<"kurz" | "gründlich">("kurz");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<ResearchCard[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const now = nowSec();

  const loadHistory = useCallback(async () => {
    try {
      const board = await fetchJSON<BoardLike>(
        "/api/plugins/kanban/board?tenant=research&card_diagnostics=summary&card_body=none",
      );
      const flat = board.columns.flatMap((c) => c.tasks);
      flat.sort((a, b) => b.created_at - a.created_at);
      setHistory(flat.slice(0, 30));
      setHistoryError(null);
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // Erst-Load per setTimeout(0) — Hauskonvention (TriageStrip), s.o.
    const firstLoad = window.setTimeout(() => void loadHistory(), 0);
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void loadHistory();
    }, 10000);
    return () => {
      window.clearTimeout(firstLoad);
      window.clearInterval(id);
    };
  }, [loadHistory]);

  const submit = useCallback(async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const title = q.split("\n")[0].slice(0, 140);
      const res = await fetchJSON<{ task?: { id?: string } }>("/api/plugins/kanban/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          body: `${q}\n\n---\n${DEPTH_HINT[depth]}`,
          assignee: "research",
          tenant: "research",
          priority: depth === "gründlich" ? 1 : 0,
          model_override: model.trim() || null,
          idempotency_key: buildResearchIdempotencyKey(),
          notify_home: false,
        }),
      });
      setNotice(res.task?.id ? t.submitted(res.task.id) : null);
      setQuestion("");
      void loadHistory();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [question, busy, depth, model, loadHistory]);

  const count = useMemo(() => history?.length ?? 0, [history]);

  return (
    <div className="space-y-4">
      <Hero eyebrow={t.eyebrow} title={t.title} subtitle={t.subtitle} count={count} countHint={t.history} tone="cyan">
        <div className="space-y-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            aria-label={t.questionLabel}
            placeholder={t.questionPlaceholder}
            rows={3}
            className="w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-3 py-2 text-sm text-white placeholder:hc-dim"
          />
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={depth}
              aria-label={t.depthLabel}
              onChange={(e) => setDepth(e.target.value as "kurz" | "gründlich")}
              className="rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs text-white"
            >
              <option value="kurz">{t.depthShort}</option>
              <option value="gründlich">{t.depthDeep}</option>
            </select>
            <ModelPicker value={model} onChange={setModel} label={t.modelLabel} placeholder="Modell-Override" />
            <button
              type="button"
              onClick={() => void submit()}
              disabled={busy || !question.trim()}
              className="inline-flex min-h-11 items-center rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-4 py-1.5 text-sm font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
            >
              {busy ? t.submitting : t.submit}
            </button>
          </div>
        </div>
      </Hero>

      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {notice ? <ToneCallout tone="emerald">{notice}</ToneCallout> : null}

      <FleetPanel eyebrow={t.history} meta={t.historyHint}>
        {historyError ? <ToneCallout tone="red">{t.loadError}<br />{historyError}</ToneCallout> : null}
        {history !== null && history.length === 0 ? (
          <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
        ) : (
          <ul className="space-y-1.5">
            {(history ?? []).map((card) => (
              <ResearchEntry key={card.id} card={card} now={now} />
            ))}
          </ul>
        )}
      </FleetPanel>
    </div>
  );
}
