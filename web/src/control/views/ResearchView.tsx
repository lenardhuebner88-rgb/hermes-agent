import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { TriangleAlert } from "lucide-react";
import { Hero } from "../components/Hero";
import { FleetEmptyState, FleetPanel, SignalChip, SignalLabel } from "../components/leitstand";
import { Eyebrow } from "../components/primitives";
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
    <li className="rounded-card border border-line px-3 py-2.5">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="flex w-full flex-wrap items-center gap-2 text-left">
        <span className="min-w-0 flex-1 basis-64 truncate text-sec font-medium text-ink">{card.title}</span>
        <SignalChip tone={done || running ? "ok" : "neutral"} label={taskStatusLabel[card.status as keyof typeof taskStatusLabel] ?? card.status} />
        <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{fmtClock(card.created_at)}</span>
      </button>
      {/* Phase-A-Fortschritt erbt die Karte gratis: Note + ehrliche ETA. */}
      {worker ? (
        <div className="mt-1.5 text-sec text-ink-2">
          {worker.last_heartbeat_note ? <><Eyebrow className="mr-1 inline">{t.doingNow}:</Eyebrow><span>{worker.last_heartbeat_note}</span></> : null}
          {worker.eta_p50_seconds ? (
            <span className="text-ink-3"> · {t.etaLine(fmtDur(worker.eta_p50_seconds), fmtDur(Math.max(0, now - worker.started_at)))}</span>
          ) : null}
        </div>
      ) : null}
      {open ? (
        <div className="mt-2 space-y-3 border-t border-line pt-2">
          {detail?.task?.body ? (
            <details>
              <summary className="cursor-pointer text-sec text-ink-3">{t.question}</summary>
              <p className="mt-1 whitespace-pre-wrap text-sec text-ink-2">{detail.task.body}</p>
            </details>
          ) : null}
          {answer ? (
            <div>
              {answer.author ? (
                <Eyebrow className="mb-1">{t.answerMeta(answer.author, answer.at ? fmtClock(answer.at) : "")}</Eyebrow>
              ) : null}
              <ProseMarkdown>{answer.body}</ProseMarkdown>
            </div>
          ) : (
            <p className="text-sec text-ink-3">{t.noAnswer}</p>
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
            className="w-full rounded-card border border-line bg-surface-2 px-3 py-2 text-sm text-ink placeholder:text-ink-3"
          />
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={depth}
              aria-label={t.depthLabel}
              onChange={(e) => setDepth(e.target.value as "kurz" | "gründlich")}
              className="rounded-card border border-line bg-surface-2 px-2 py-1.5 text-xs text-ink"
            >
              <option value="kurz">{t.depthShort}</option>
              <option value="gründlich">{t.depthDeep}</option>
            </select>
            <ModelPicker value={model} onChange={setModel} label={t.modelLabel} placeholder="Modell-Override" />
            <button
              type="button"
              onClick={() => void submit()}
              disabled={busy || !question.trim()}
              className="inline-flex min-h-12 items-center rounded-card border border-live bg-live/10 px-4 py-1.5 text-sm font-medium text-live disabled:opacity-50"
            >
              {busy ? t.submitting : t.submit}
            </button>
          </div>
        </div>
      </Hero>

      {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {notice ? <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2"><SignalLabel tone="ok" label="Gestartet" />{notice}</div> : null}

      <FleetPanel eyebrow={t.history} meta={t.historyHint}>
        {historyError ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span>{t.loadError}<br />{historyError}</span></div> : null}
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
