/**
 * Landing-Pack (deterministic) — Karten-Sektion und Drawer-Sektionen.
 *
 * Vertrag: hermes_cli/control_loops.py (_landing_control_payload für die
 * Summary-Felder, pack_detail-Landing-Block für den Drawer). Alle Felder sind
 * additiv-optional: Komponenten rendern "—"/Empty-States statt zu werfen.
 *
 * DESIGN.md: mono = data only (SHAs, Counts, ISO-Zeiten via font-data);
 * Status nie farballeinig (immer Textlabel); Labels/Prosa ohne Mono.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Eye, XCircle } from "lucide-react";
import type {
  LoopDetailResponse,
  LoopLandingRecoveryEntry,
  LoopPackSummary,
} from "../../lib/types";
import { formatLoopTimestamp } from "../../lib/loopTime";
import { de } from "../../i18n/de";

const t = de.loops;

const inkSoft: React.CSSProperties = { color: "var(--ln-ink-soft)" };
const inkMute: React.CSSProperties = { color: "var(--ln-ink-mute)" };

function SectionCaption({ children }: { children: ReactNode }) {
  return (
    <p className="text-[11px] uppercase tracking-[0.14em]" style={inkSoft}>
      {children}
    </p>
  );
}

function fmtMaybeTime(value: string | null | undefined): string {
  if (!value) return "—";
  return formatLoopTimestamp(value) ?? value;
}

function shortSha(sha: string | null | undefined): string {
  if (!sha) return "—";
  return sha.slice(0, 7);
}

/** loop/<task-id> → Ursprungskarte im Fleet-Board; sonst null. */
export function landingOriginTaskId(branch: string): string | null {
  const match = /^loop\/(t_[A-Za-z0-9]+)$/.exec(branch);
  return match ? match[1] : null;
}

function baselineLabel(pack: LoopPackSummary): { text: string; ok: boolean | null } {
  if (pack.baseline_ok === true) return { text: t.landingBaselineOk, ok: true };
  if (pack.baseline_ok === false) return { text: t.landingBaselineFail, ok: false };
  return { text: t.landingBaselineUnknown, ok: null };
}

function lastResultLabel(value: string | null | undefined): string {
  if (value === "green") return t.landingResultGreen;
  if (value === "held") return t.landingResultHeld;
  return t.landingResultUnknown;
}

function baselineSourceLabel(
  source: LoopPackSummary["baseline_source"],
): string {
  if (source === "nightly") return t.landingBaselineSourceNightly;
  if (source === "landing") return t.landingBaselineSourceLanding;
  return t.landingBaselineSourceNone;
}

// ── Karten-Sektion ──────────────────────────────────────────────────────────

export function LandingCardSection({
  pack,
  busy,
  onToggleAutomation,
  onPreview,
}: {
  pack: LoopPackSummary;
  busy: boolean;
  onToggleAutomation: (enabled: boolean) => void;
  onPreview: () => void;
}) {
  const automationOn = pack.automation_enabled === true;
  const baseline = baselineLabel(pack);
  const queue = pack.queue_summary;
  const queueTotal = queue?.total ?? 0;
  const windowCloses = pack.collection_window?.closes_at ?? null;
  return (
    <section
      aria-label={t.landingSectionLabel}
      className="space-y-2 rounded-lg border p-3"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-surface)" }}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            role="switch"
            aria-checked={automationOn}
            aria-label={t.landingAutomation}
            disabled={busy}
            onClick={() => onToggleAutomation(!automationOn)}
            className="inline-flex min-h-12 items-center gap-2 rounded-lg border px-3 text-xs font-semibold disabled:opacity-50"
            style={{
              borderColor: automationOn ? "var(--ln-ok)" : "var(--ln-line)",
              color: "var(--ln-ink)",
            }}
          >
            <span
              aria-hidden
              className="relative inline-flex h-4 w-7 items-center rounded-full border"
              style={{
                borderColor: automationOn ? "var(--ln-ok)" : "var(--ln-ink-mute)",
                background: automationOn ? "var(--ln-ok)" : "transparent",
              }}
            >
              <span
                className="absolute h-3 w-3 rounded-full"
                style={{
                  background: automationOn ? "var(--ln-sodium-ink)" : "var(--ln-ink-mute)",
                  left: automationOn ? "calc(100% - 0.875rem)" : "0.125rem",
                }}
              />
            </span>
            {t.landingAutomation}: {automationOn ? t.landingAutomationOn : t.landingAutomationOff}
          </button>
        </div>
        <button
          type="button"
          disabled={busy}
          title={t.landingPreviewHint}
          onClick={onPreview}
          className="inline-flex min-h-12 items-center justify-center gap-2 rounded-lg border px-3 text-xs font-semibold disabled:opacity-50"
          style={{ borderColor: "var(--ln-ink-soft)", color: "var(--ln-ink-soft)" }}
        >
          <Eye size={14} aria-hidden />
          {t.landingPreview}
        </button>
      </div>
      <p className="text-[11px]" style={inkMute}>
        {t.landingAutomationHint}
      </p>
      <dl className="space-y-1 text-xs" style={inkSoft}>
        <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>{t.landingQueueLabel}</dt>
          <dd className="font-data" style={{ color: "var(--ln-ink)" }}>
            {queueTotal > 0
              ? t.landingQueue(
                  queueTotal,
                  queue?.landed ?? 0,
                  queue?.cleaned ?? 0,
                  queue?.parked ?? 0,
                )
              : t.landingQueueEmpty}
          </dd>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>{t.landingBaselineLabel}</dt>
          <dd className="font-data" style={{ color: "var(--ln-ink)" }}>
            {shortSha(pack.baseline_sha)}
          </dd>
          <dd className="inline-flex items-center gap-1">
            {baseline.ok === true ? (
              <CheckCircle2 size={12} aria-hidden style={{ color: "var(--ln-ok)" }} />
            ) : baseline.ok === false ? (
              <XCircle size={12} aria-hidden style={{ color: "var(--ln-fail)" }} />
            ) : null}
            {baseline.text}
          </dd>
        </div>
        {pack.baseline_reason ? (
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <dt>{t.landingBaselineEvidenceLabel}</dt>
            <dd className="min-w-0 break-words" style={{ color: "var(--ln-ink)" }}>
              {baselineSourceLabel(pack.baseline_source)}: {pack.baseline_reason}
            </dd>
          </div>
        ) : null}
        <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>{t.landingNextTriggerLabel}</dt>
          <dd className="font-data" style={{ color: "var(--ln-ink)" }}>
            {pack.next_trigger_at ? fmtMaybeTime(pack.next_trigger_at) : t.landingNoTrigger}
          </dd>
          {windowCloses ? (
            <dd className="font-data">{t.landingWindowOpen(fmtMaybeTime(windowCloses))}</dd>
          ) : null}
        </div>
        <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>{t.landingLastResultLabel}</dt>
          <dd role="status" style={{ color: "var(--ln-ink)" }}>{lastResultLabel(pack.last_result)}</dd>
        </div>
      </dl>
    </section>
  );
}

// ── Drawer-Sektionen ────────────────────────────────────────────────────────

function recoveryStateLabel(state: LoopLandingRecoveryEntry["state"]): string {
  if (state === "started") return t.landingRecoveryStarted;
  if (state === "exhausted") return t.landingRecoveryExhausted;
  return t.landingRecoveryRequested;
}

function recoveryAttemptLabel(state: LoopLandingRecoveryEntry["state"]): string {
  // requested = Anfrage eingereiht (Versuch 0), started/exhausted = der eine
  // erlaubte Recovery-Versuch wurde gestartet/verbraucht (Versuch 1).
  return state === "requested" ? t.landingRecoveryAttempt0 : t.landingRecoveryAttempt1;
}

export function LandingDetailSections({ detail }: { detail: LoopDetailResponse }) {
  const candidates = detail.candidates ?? [];
  const gateStages = detail.gate_stages ?? [];
  const recovery = detail.recovery ?? [];
  const triggerHistory = detail.trigger_history ?? [];
  const preview = detail.preview;
  return (
    <>
      <div>
        <SectionCaption>{t.landingCandidates}</SectionCaption>
        {candidates.length > 0 ? (
          <ul className="mt-1 space-y-1" style={inkSoft}>
            {candidates.map((candidate) => {
              const taskId = landingOriginTaskId(candidate.branch);
              return (
                <li key={candidate.branch} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-data" style={{ color: "var(--ln-ink)" }}>
                    {candidate.branch}
                  </span>
                  <span className="font-data">
                    {t.landingCandidateDelta(candidate.ahead ?? 0, candidate.behind ?? 0)}
                  </span>
                  <span className="font-data">{shortSha(candidate.head)}</span>
                  {taskId ? (
                    <Link
                      to={`/control/fleet?task=${taskId}`}
                      className="underline decoration-dotted underline-offset-2"
                      style={{ color: "var(--ln-sodium)" }}
                    >
                      {t.landingOriginTask}
                    </Link>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="mt-1" style={inkSoft}>{t.landingNoCandidates}</p>
        )}
      </div>
      <div>
        <SectionCaption>{t.landingGateStages}</SectionCaption>
        {gateStages.length > 0 ? (
          <ol className="mt-1 flex flex-wrap items-center gap-x-1 gap-y-1" style={inkSoft}>
            {gateStages.map((stage, index) => (
              <li key={stage} className="flex items-center gap-1">
                {index > 0 ? <span aria-hidden>→</span> : null}
                <span className="font-data">{stage}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-1" style={inkSoft}>{t.landingNoGateStages}</p>
        )}
      </div>
      <div>
        <SectionCaption>{t.landingRecovery}</SectionCaption>
        {recovery.length > 0 ? (
          <ul className="mt-1 space-y-1" style={inkSoft}>
            {recovery.map((entry) => (
              <li key={`${entry.task_id}:${entry.fingerprint}`} className="flex flex-wrap items-baseline gap-x-2">
                <Link
                  to={`/control/fleet?task=${entry.task_id}`}
                  className="underline decoration-dotted underline-offset-2 font-data"
                  style={{ color: "var(--ln-sodium)" }}
                >
                  {entry.task_id}
                </Link>
                <span style={{ color: "var(--ln-ink)" }}>{recoveryStateLabel(entry.state)}</span>
                <span>{recoveryAttemptLabel(entry.state)}</span>
                {entry.fingerprint ? (
                  <span className="font-data" title={entry.fingerprint}>
                    {shortSha(entry.fingerprint)}
                  </span>
                ) : null}
                {entry.failing_gate ? <span className="font-data">{entry.failing_gate}</span> : null}
                {entry.failure_class ? <span className="font-data">{entry.failure_class}</span> : null}
                <span className="font-data">{fmtMaybeTime(entry.requested_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1" style={inkSoft}>{t.landingNoRecovery}</p>
        )}
      </div>
      <div>
        <SectionCaption>{t.landingTriggerHistory}</SectionCaption>
        {triggerHistory.length > 0 || detail.followup_pending ? (
          <ul className="mt-1 space-y-0.5 font-data" style={inkSoft}>
            {detail.followup_pending ? <li>{t.landingFollowupPending}</li> : null}
            {triggerHistory.map((signal, index) => (
              <li key={`${signal}-${index}`}>{signal}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-1" style={inkSoft}>{t.landingNoTriggers}</p>
        )}
      </div>
      <div>
        <SectionCaption>{t.landingPreviewTitle}</SectionCaption>
        {preview ? (
          <div className="mt-1 space-y-1" style={inkSoft}>
            <p role="status">
              {preview.running
                ? t.landingPreviewRunning
                : preview.finished_at
                  ? t.landingPreviewFinished(preview.rc ?? -1)
                  : t.landingPreviewStale}
              <span className="font-data"> · {fmtMaybeTime(preview.started_at)}</span>
            </p>
            {preview.output_tail ? (
              <pre
                className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border p-2 font-data text-[11px]"
                style={{ borderColor: "var(--ln-line)", color: "var(--ln-ink)" }}
              >
                {preview.output_tail}
              </pre>
            ) : null}
          </div>
        ) : (
          <p className="mt-1" style={inkSoft}>{t.landingPreviewNone}</p>
        )}
      </div>
    </>
  );
}
