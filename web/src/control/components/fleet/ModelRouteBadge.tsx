import type { ModelRouteState } from "../../lib/types";
import { de } from "../../i18n/de";

interface ModelRouteBadgeProps {
  requestedProvider?: string | null;
  requestedModel?: string | null;
  activeProvider?: string | null;
  activeModel?: string | null;
  modelState?: ModelRouteState | null;
  modelSource?: string | null;
  observedAt?: number | null;
  /** Ob zu diesem Step bereits ein Run existiert. Ohne Run bleibt fehlende Telemetrie neutral. */
  hasRun?: boolean;
  className?: string;
}

const STATE_LABEL: Record<ModelRouteState, string> = {
  planned: de.worker.modelRoutePlanned,
  in_flight: de.worker.modelRouteInFlight,
  confirmed: de.worker.modelRouteConfirmed,
  unknown: "unbekannt",
};

const STATE_TONE: Record<ModelRouteState, string> = {
  planned: "text-ink-2",
  in_flight: "text-live",
  confirmed: "text-status-ok",
  unknown: "text-status-warn",
};

function observedLabel(observedAt?: number | null): string | null {
  if (observedAt == null || !Number.isFinite(observedAt)) return null;
  return new Date(observedAt * 1000).toLocaleString("de-DE");
}

export function ModelRouteBadge({
  requestedProvider,
  requestedModel,
  activeProvider,
  activeModel,
  modelState,
  modelSource,
  observedAt,
  hasRun = true,
  className = "",
}: ModelRouteBadgeProps) {
  const provider = activeProvider || requestedProvider || null;
  const model = activeModel || requestedModel || null;
  const legacy = modelSource === "legacy_inferred";
  const state = modelState ?? "unknown";
  const stateLabel = legacy ? de.worker.modelRouteInferred : STATE_LABEL[state];
  const observed = observedLabel(observedAt);
  const provenance = [
    de.worker.modelRouteSource(modelSource || de.worker.modelRouteMissing),
    de.worker.modelRouteObserved(observed || de.worker.modelRouteMissing),
  ].join(" · ");

  if (!hasRun) {
    return (
      <span
        className={`inline-flex max-w-full flex-wrap items-center rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs text-ink-3 ${className}`}
        aria-label={de.worker.modelRouteNoRun}
        title={de.worker.modelRouteNoRun}
      >
        {de.worker.modelRouteNoRun}
      </span>
    );
  }

  if (!provider || !model) {
    return (
      <span
        className={`inline-flex max-w-full flex-wrap items-center rounded-card border border-status-warn/30 bg-status-warn/10 px-2 py-0.5 text-xs text-status-warn ${className}`}
        aria-label={`${de.worker.modelRouteUnknown}. ${provenance}`}
        title={provenance}
      >
        {de.worker.modelRouteUnknown}
      </span>
    );
  }

  return (
    <span
      className={`inline-flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs ${className}`}
      aria-label={`${provider} · ${model} · ${stateLabel}. ${provenance}`}
      title={provenance}
    >
      <span className="break-all font-data text-ink">{provider} · {model}</span>
      <span className={STATE_TONE[legacy ? "unknown" : state]}>{stateLabel}</span>
    </span>
  );
}
