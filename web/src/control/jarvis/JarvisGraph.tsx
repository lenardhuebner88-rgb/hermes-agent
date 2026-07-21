/**
 * JarvisGraph — Daten-Wrapper um JarvisGraphCanvas (F2).
 *
 * Behält die Fallback-Hierarchie (usePaGraphView: Mock/STALE/keep-last-good),
 * Footer-Tags und das aria-Zustandslabel. Rendert die Canvas; Pure-Functions
 * werden aus graphEngine re-exportiert (API-Kompatibilität).
 */
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { de } from "../i18n/de";
import {
  BIG_LABEL_WEIGHT,
  createSimulation,
  edgePath,
  edgeTier,
  GLOW_WEIGHT,
  LABELS_PER_CLUSTER,
  LABEL_WEIGHT,
  MAX_LABEL_CHARS,
  nodeRadius,
  openGraphRef,
  ORB_WEIGHT,
  truncateLabel,
} from "./graphEngine";
import { JarvisGraphCanvas } from "./JarvisGraphCanvas";
import { JARVIS_BRAIN_MOCKTAG } from "./mockContent";
import { formatGraphStand, usePaGraphView } from "./usePaGraph";

const t = de.jarvis;

/* ── Re-exports: Pure-Functions/Konstanten leben in graphEngine (F1).
 *  Bestehende Importe aus ./JarvisGraph bleiben API-kompatibel. ── */
export {
  BIG_LABEL_WEIGHT,
  createSimulation,
  edgePath,
  edgeTier,
  GLOW_WEIGHT,
  LABELS_PER_CLUSTER,
  LABEL_WEIGHT,
  MAX_LABEL_CHARS,
  nodeRadius,
  openGraphRef,
  ORB_WEIGHT,
  truncateLabel,
};

export function JarvisGraph() {
  const view = usePaGraphView();
  const { graph } = view;
  const navigate = useNavigate();

  // Teilquellen-Fehler dezent: Console (Tooltip hängt am Footer-Tag). Kein Panel.
  const sourceErrorKey = view.sourceErrors.map((e) => `${e.source}: ${e.error}`).join("\n");
  useEffect(() => {
    if (sourceErrorKey) console.warn(`[JarvisGraph] Teilquellen-Fehler:\n${sourceErrorKey}`);
  }, [sourceErrorKey]);

  const ariaLabel = view.isLive
    ? t.graphAriaLive(graph.nodes.length) + (view.isStale ? t.graphAriaStaleSuffix : "")
    : t.graphAriaMock;

  const statusLive = view.isLive
    ? t.graphAriaLive(graph.nodes.length) + (view.isStale ? t.graphAriaStaleSuffix : "")
    : t.graphAriaMock;

  return (
    <JarvisGraphCanvas
      graph={graph}
      ariaLabel={ariaLabel}
      statusLive={statusLive}
      onNodeOpen={(node) => {
        openGraphRef(node.href ?? node.ref, {
          navigate,
          assign: (url) => window.location.assign(url),
        });
      }}
    />
  );
}

/** Desktop-Footer-Tag (jv-gtag): zustandsabhängig live vs. mock (S2.7). */
export function JarvisGraphTag() {
  const view = usePaGraphView();
  const errorsTitle =
    view.sourceErrors.length > 0
      ? t.graphSourceErrorsTitle(view.sourceErrors.map((e) => `${e.source}: ${e.error}`).join("\n"))
      : undefined;
  return (
    <div className="jv-gtag" title={errorsTitle}>
      GRAPH · <b>{view.isLive ? t.graphStateLive : t.graphStateMock}</b>
      {view.isLive ? (
        <>
          {t.graphTagLiveTail(view.graph.nodes.length, formatGraphStand(view.graph.generated_at))}
          {view.isStale ? t.graphTagStaleSuffix : null}
        </>
      ) : (
        t.graphTagMockTail
      )}
    </div>
  );
}

/** Mobiler Inline-Tag in .jv-stats: Mocktag-Bestandstext vs. Live-Kurzform. */
export function JarvisGraphStatsTag() {
  const view = usePaGraphView();
  return (
    <span className="jv-mocktag">
      {view.isLive ? t.graphMocktagLive(view.graph.nodes.length) : JARVIS_BRAIN_MOCKTAG}
    </span>
  );
}
