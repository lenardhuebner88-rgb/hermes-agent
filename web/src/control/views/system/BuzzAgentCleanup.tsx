import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Card, Eyebrow } from "../../components/primitives";
import { KpiTile, SectionHeader, SignalChip, type SignalTone } from "../../components/leitstand";
import { Overlay } from "../../components/Overlay";
import { useBuzzCleanupRun, useBuzzCleanupStatus } from "../../hooks/systemReleaseHealth";
import type {
  BuzzCleanupReceipt,
  BuzzCleanupUnitMetrics,
} from "../../lib/types";
import { nowSec } from "../../lib/derive";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------------------------
 * Buzz-Agent-Cleanup — genehmigte A+C-Komposition (Design-Board-Referenz
 * vault/Design/buzz-agent-cleanup-20260801/mockups/index.html#a + #c +
 * #a-confirm). Kompakte Wartungsleiste direkt unter dem Systempuls, dauer-
 * hafte Quittung (C) aus dem Backend-Receipt, Overlay-Bestätigungsdialog.
 * Doctrine: unbekannte Werte bleiben "unbekannt", nie 0 (00-Canon rule 3).
 * ------------------------------------------------------------------------- */

/** Wörtliche Warnung im Bestätigungsdialog (AC-4). */
export const BUZZ_CLEANUP_WARNING =
  "Laufende Antworten können verloren gehen. Jeder Agent kann rund 45 Sekunden keine Nachrichten empfangen.";

const PRIMARY_ACTION_CLASS = cn(
  "inline-flex min-h-[44px] w-full items-center justify-center gap-2 rounded-full border px-4 text-sm font-medium transition",
  "border-action/40 bg-action/10 text-action hover:bg-action/15",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "disabled:cursor-not-allowed disabled:opacity-50 tab:w-auto",
);

const GHOST_ACTION_CLASS = cn(
  "inline-flex min-h-[44px] items-center justify-center rounded-full border border-line bg-surface-2 px-4 text-sm font-medium text-ink-2 transition",
  "hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

/** Byte-Format der Referenz: GB mit einer Dezimale ab 1 GiB, sonst MB.
 *  Unbekannt bleibt unbekannt (null → "unbekannt", niemals 0). */
export function fmtBuzzBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "unbekannt";
  if (value >= 1024 ** 3) {
    return `${(value / 1024 ** 3).toLocaleString("de-DE", { maximumFractionDigits: 1 })} GB`;
  }
  return `${Math.round(value / 1024 ** 2)} MB`;
}

function fmtDeltaPct(before: number | null, after: number | null): string | null {
  if (before == null || after == null || before <= 0) return null;
  const pct = Math.round(((before - after) / before) * 100);
  return `−${pct.toLocaleString("de-DE")} %`;
}

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "unbekannt";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "unbekannt";
  return new Date(ms).toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Nächster Timer-Lauf: relativ ("in 6h 12m") + absolut als Hint. */
function fmtNextRun(iso: string | null, nowMs: number): { value: string; delta?: string } {
  if (!iso) return { value: "unbekannt" };
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return { value: "unbekannt" };
  const abs = new Date(ms).toLocaleString("de-DE", {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
  const diffMin = Math.round((ms - nowMs) / 60_000);
  if (diffMin < 0) return { value: abs, delta: "überfällig" };
  const hours = Math.floor(diffMin / 60);
  const mins = diffMin % 60;
  const rel = hours > 0 ? `in ${hours}h ${String(mins).padStart(2, "0")}m` : `in ${mins}m`;
  return { value: rel, delta: abs };
}

/** Ehrliche Cgroup-Summe: nur wenn JEDE Unit einen bekannten Wert hat,
 *  sonst null → "unbekannt" (niemals Teilsummen als Gesamt verkaufen). */
export function buzzCgroupSum(
  snapshots: Record<string, BuzzCleanupUnitMetrics>,
  units: string[],
): number | null {
  let sum = 0;
  for (const unit of units) {
    const value = snapshots[unit]?.cgroup_process_count;
    if (value == null) return null;
    sum += value;
  }
  return sum;
}

export type BuzzUnitProgress = "done" | "restarting" | "pending" | "unknown";

/** Fortschritt je Unit während eines Laufs (AC-5). Ehrlich abgeleitet:
 *  die PID hat sich gegenüber der beim Start erfassten Basis geändert →
 *  neu gestartet; Unit nicht active → Restart läuft gerade. Ohne Start-
 *  Basis (Reload mitten im Lauf, fremd gestartet) bleibt der Fortschritt
 *  unbekannt — dann zeigt die Zeile nur den Live-Status. */
export function buzzUnitProgress(
  metrics: BuzzCleanupUnitMetrics | undefined,
  baselinePid: number | null | undefined,
): BuzzUnitProgress {
  if (baselinePid === undefined) return "unknown";
  if (!metrics) return "pending";
  if (metrics.main_pid != null && metrics.main_pid !== baselinePid) return "done";
  if (baselinePid == null && metrics.main_pid != null) return "done";
  if (metrics.active_state !== "active") return "restarting";
  return "pending";
}

const PROGRESS_LABEL: Record<BuzzUnitProgress, { label: string; tone: SignalTone }> = {
  done: { label: "neu gestartet", tone: "ok" },
  restarting: { label: "wird neu gestartet", tone: "warn" },
  pending: { label: "ausstehend", tone: "neutral" },
  unknown: { label: "live", tone: "neutral" },
};

function receiptVerdict(receipt: BuzzCleanupReceipt): { tone: SignalTone; label: string } {
  if (receipt.error || receipt.exit_code === 1) return { tone: "alert", label: "Fehler" };
  if (receipt.failed_units.length > 0) return { tone: "warn", label: "Teilerfolg" };
  if (receipt.exit_code === 0) return { tone: "ok", label: "Voller Erfolg" };
  return { tone: "neutral", label: "Beendet" };
}

function ReceiptRow({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-micro text-ink-3">{label}</span>
      <span className={cn("text-right text-sec text-ink", mono && "font-data")}>{value}</span>
    </div>
  );
}

function BuzzCleanupReceiptCard({ receipt }: { receipt: BuzzCleanupReceipt }) {
  const verdict = receiptVerdict(receipt);
  const doneCount = Math.max(0, receipt.targets.length - receipt.failed_units.length);
  const quote =
    receipt.targets.length > 0
      ? `${doneCount}/${receipt.targets.length} neu gestartet`
      : "keine Ziele";
  const delta = fmtDeltaPct(receipt.total_memory_current_before, receipt.total_memory_current_after);
  const cgroupBefore = buzzCgroupSum(receipt.before, receipt.targets);
  const cgroupAfter = buzzCgroupSum(receipt.after, receipt.targets);
  return (
    <Card surface="card" className="space-y-2.5 p-3 tab:p-4">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>Letzte Quittung</Eyebrow>
        <SignalChip tone={verdict.tone} label={verdict.label} />
      </div>
      <div className="space-y-1.5">
        <ReceiptRow label="Zeitpunkt" value={fmtDateTime(receipt.finished_at ?? receipt.started_at)} />
        <ReceiptRow label="Erfolgsquote" value={quote} />
        <ReceiptRow
          label="MemoryCurrent"
          value={
            `${fmtBuzzBytes(receipt.total_memory_current_before)} → ${fmtBuzzBytes(receipt.total_memory_current_after)}` +
            (delta ? ` (${delta})` : "")
          }
        />
        <ReceiptRow
          label="Cgroup-Prozesse"
          value={`${cgroupBefore == null ? "unbekannt" : cgroupBefore} → ${cgroupAfter == null ? "unbekannt" : cgroupAfter}`}
        />
        {receipt.error ? (
          <ReceiptRow label="Fehlercode" value={receipt.error.code || "unbekannt"} />
        ) : null}
      </div>
      {receipt.failed_units.length > 0 ? (
        <div className="space-y-1 border-t border-line-soft pt-2">
          <p className="text-micro text-ink-3">Fehlgeschlagen</p>
          <ul className="space-y-0.5">
            {receipt.failed_units.map((unit) => (
              <li key={unit} className="font-data text-micro text-status-warn">
                {unit}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

export function BuzzAgentCleanup() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [ack, setAck] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  /** PID-Basis beim clientseitigen Start; null bei Reload/externem Start. */
  const [baseline, setBaseline] = useState<Record<string, number | null> | null>(null);

  const status = useBuzzCleanupStatus();
  const running = status.data?.running === true;
  const effectiveStatus = status.data;
  const { start, starting } = useBuzzCleanupRun();

  // Lauf-Ende erkennen: Basis verwerfen, 409-Notiz aufräumen. Die Quittung
  // kommt mit dem nächsten Poll aus dem Backend (überlebt Reloads, AC-3).
  const wasRunningRef = useRef(false);
  useEffect(() => {
    if (wasRunningRef.current && !running) {
      setBaseline(null);
      setNotice(null);
    }
    wasRunningRef.current = running;
  }, [running]);

  const openDialog = () => {
    setAck(false);
    setStartError(null);
    setDialogOpen(true);
  };

  const confirmStart = async () => {
    setStartError(null);
    // Start-Basis aus dem letzten bekannten Status, BEVOR der POST rausgeht.
    const basis: Record<string, number | null> = {};
    const targets = effectiveStatus?.units ?? [];
    for (const unit of targets) {
      basis[unit] = effectiveStatus?.unit_metrics[unit]?.main_pid ?? null;
    }
    const result = await start();
    if (result.ok) {
      setBaseline(basis);
      setNotice(null);
      setDialogOpen(false);
      return;
    }
    if (result.code === "already_running") {
      // 409: kein zweiter Start — derselbe Status-Poll übernimmt die
      // Darstellung des laufenden Cleanups (AC-5).
      setBaseline(null);
      setNotice("Ein Cleanup läuft bereits — der Fortschritt wird übernommen.");
      setDialogOpen(false);
      return;
    }
    if (result.code === "target_mismatch") {
      setStartError("Die Zielmenge hat sich geändert — der Status wird neu geladen. Bitte erneut prüfen.");
      return;
    }
    if (result.code === "no_targets") {
      setStartError("Keine Buzz-Agenten gefunden — es gibt nichts aufzuräumen.");
      return;
    }
    setStartError(result.detail ?? "Start fehlgeschlagen.");
  };

  const busy = status.loading && !effectiveStatus;
  const loadError = status.error && !effectiveStatus ? status.error : null;

  const mismatch = useMemo(() => {
    if (!effectiveStatus || effectiveStatus.target_sets_equal) return null;
    const parts: string[] = [];
    const uwc = effectiveStatus.units_without_config;
    const cwu = effectiveStatus.configs_without_unit;
    if (uwc.length > 0) parts.push(`${uwc.length} Units ohne Config (${uwc.join(", ")})`);
    if (cwu.length > 0) parts.push(`${cwu.length} Configs ohne Unit (${cwu.join(", ")})`);
    return parts.length > 0 ? parts.join("; ") : "Unit- und Config-Menge fallen auseinander";
  }, [effectiveStatus]);

  const empty = effectiveStatus != null && effectiveStatus.units.length === 0;
  const canStart =
    effectiveStatus != null && !running && !mismatch && !empty && effectiveStatus.units.length > 0;

  const nextRun = fmtNextRun(effectiveStatus?.next_timer_run ?? null, nowSec() * 1000);
  const progressUnits = baseline != null ? Object.keys(baseline) : (effectiveStatus?.units ?? []);
  const doneCount =
    baseline != null
      ? progressUnits.filter(
          (unit) => buzzUnitProgress(effectiveStatus?.unit_metrics[unit], baseline[unit]) === "done",
        ).length
      : null;

  return (
    <section aria-label="Wartung Buzz-Agenten" className="space-y-2">
      <SectionHeader label="Wartung" meta="Buzz-Agenten" />
      <Card surface="raised" className="space-y-3 p-3 tab:p-4">
        {busy ? (
          <p className="text-sec text-ink-2">Wartungsstatus wird geladen …</p>
        ) : loadError ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sec text-status-alert" role="alert">
              Wartungsstatus nicht lesbar: {loadError}
            </p>
            <button type="button" className={GHOST_ACTION_CLASS} onClick={() => status.reload()}>
              Erneut versuchen
            </button>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 tab:grid-cols-3">
              <KpiTile
                label="Zielmenge"
                value={effectiveStatus?.units.length ?? "–"}
                suffix={effectiveStatus ? (effectiveStatus.units.length === 1 ? "Unit" : "Units") : undefined}
              />
              <KpiTile
                label="Gesamt-RSS"
                value={fmtBuzzBytes(effectiveStatus?.total_memory_current ?? null)}
              />
              <KpiTile
                label="Nächster Lauf"
                value={nextRun.value}
                delta={nextRun.delta}
                className="col-span-2 tab:col-span-1"
              />
            </div>

            {running ? (
              <div className="space-y-2" aria-live="polite">
                {notice ? (
                  <p className="text-sec text-status-warn" role="status">
                    {notice}
                  </p>
                ) : null}
                <div className="flex items-center gap-2">
                  <Loader2 aria-hidden className="size-4 animate-spin text-action motion-reduce:animate-none" />
                  <p className="text-sec font-medium text-ink">
                    Cleanup läuft
                    {doneCount != null
                      ? ` — ${doneCount}/${progressUnits.length} neu gestartet`
                      : " — Live-Status (Start-Basis unbekannt)"}
                  </p>
                </div>
                <ul className="space-y-1">
                  {progressUnits.map((unit) => {
                    const metrics = effectiveStatus?.unit_metrics[unit];
                    const state = buzzUnitProgress(metrics, baseline?.[unit]);
                    const chip = PROGRESS_LABEL[state];
                    return (
                      <li key={unit} className="flex items-center justify-between gap-2 text-micro">
                        <span className="min-w-0 truncate font-data text-ink">{unit}</span>
                        <span className="flex shrink-0 items-center gap-2">
                          <span className="font-data text-ink-2">{fmtBuzzBytes(metrics?.memory_current ?? null)}</span>
                          <SignalChip tone={chip.tone} label={chip.label} />
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : (
              <div className="space-y-2">
                {mismatch ? (
                  <p className="text-sec text-status-warn" role="alert">
                    Zielmenge unklar: {mismatch}. Start gesperrt.
                  </p>
                ) : null}
                {empty ? (
                  <p className="text-sec text-ink-2">Keine Buzz-Agenten gefunden.</p>
                ) : null}
                <button
                  type="button"
                  className={PRIMARY_ACTION_CLASS}
                  disabled={!canStart}
                  onClick={openDialog}
                >
                  Agenten aufräumen
                </button>
              </div>
            )}
          </>
        )}
      </Card>

      {!running && effectiveStatus?.last_receipt ? (
        <BuzzCleanupReceiptCard receipt={effectiveStatus.last_receipt} />
      ) : null}

      {dialogOpen && effectiveStatus ? (
        <Overlay
          onClose={() => setDialogOpen(false)}
          ariaLabel="Buzz-Agenten aufräumen bestätigen"
          closeDisabled={starting}
          maxWidthClassName="max-w-md"
        >
          <div className="space-y-4">
            <div className="space-y-1">
              <Eyebrow>Bestätigung</Eyebrow>
              <h3 className="font-display text-emph font-semibold text-ink">
                Buzz-Agenten aufräumen?
              </h3>
              <p className="font-data text-micro text-ink-3">
                {effectiveStatus.units.length} Ziele · seriell · Readiness-Gate aktiv
              </p>
            </div>

            <ul className="max-h-48 space-y-1 overflow-y-auto rounded-card border border-line-soft bg-surface-1 p-2.5">
              {effectiveStatus.units.map((unit) => (
                <li key={unit} className="font-data text-micro text-ink">
                  {unit}
                </li>
              ))}
            </ul>

            <p className="rounded-card border border-status-warn/30 bg-status-warn/10 p-3 text-sec text-status-warn">
              {BUZZ_CLEANUP_WARNING}
            </p>

            <label className="flex min-h-[44px] cursor-pointer items-center gap-3 text-sec text-ink">
              <input
                type="checkbox"
                className="size-4 accent-action"
                checked={ack}
                onChange={(event) => setAck(event.target.checked)}
              />
              Ich habe die Warnung gelesen.
            </label>

            {startError ? (
              <p className="text-sec text-status-alert" role="alert">
                {startError}
              </p>
            ) : null}

            <div className="flex flex-col-reverse gap-2 tab:flex-row tab:justify-end">
              <button
                type="button"
                className={GHOST_ACTION_CLASS}
                onClick={() => setDialogOpen(false)}
                disabled={starting}
              >
                Abbrechen
              </button>
              <button
                type="button"
                className={PRIMARY_ACTION_CLASS}
                disabled={!ack || starting}
                onClick={() => void confirmStart()}
              >
                {starting ? "Starte …" : "Start bestätigen"}
              </button>
            </div>
          </div>
        </Overlay>
      ) : null}
    </section>
  );
}
