/**
 * JarvisGraph — Estate-Graph als Vollbild-Canvas, seit S2.7 datengetrieben
 * (GET /api/pa/graph via usePaGraph, pa-graph/v1: x/y deterministisch in der
 * 1280x820-ViewBox vorberechnet). Der A4-Look ist bewahrt: dieselben Defs
 * (die jv-orb-…/jv-neb-…-Gradients), Fog + Sternenstaub als statischer
 * Rahmen, Cluster-Auren jetzt aus dem Datenschwerpunkt statt hardcodiert,
 * ruhige Quadratic-Kanten.
 *
 * Renderer-Entscheidungen (im S2-GRAPH-FE-Report dokumentiert):
 *  - Radius linear aus weight (0.2–1.0 → ~4.2–15.0, Mock-Referenz 3.8–14.8).
 *  - Orb-Gradient ab weight ≥ 0.45, zusätzlicher Glow-Halo ab ≥ 0.5
 *    (Mock: 0.47 trug Orb, Hubs ≥ 0.5 hatten Glow).
 *  - Labels: pro Cluster die LABELS_PER_CLUSTER gewichtigsten Knoten mit
 *    weight ≥ LABEL_WEIGHT (0.45) — hält ~500 Live-Knoten auf A4-Niveau
 *    lesbar; der fokussierte Knoten ist immer gelabelt. „big" ab 0.7.
 *    Lange Live-Titel werden auf 30 Zeichen gekürzt (voller Text im Tooltip).
 *  - Kanten: M x1 y1 Q mx my x2 y2 mit leicht senkrecht versetztem
 *    Kontrollpunkt (A4-Maß 0.09); Opazität/Breite aus dem leichteren
 *    Endpunkt (≥0.55 → .34/1.2, ≥0.35 → .24/1.0, sonst .17/.8).
 *  - Fokus (A4-Idiom): Tap fokussiert (Doppelring + Kanten der Nachbarn in
 *    der Fokus-Clusterfarbe + FOKUS-Tag); erneuter Tap auf den fokussierten
 *    Knoten öffnet sein href — /control/... per SPA-Router, /api/... per
 *    voller Navigation; vault:///memory:// sind reine Anzeige-Refs.
 *
 * Fallback-Hierarchie: usePaGraphView (keep-last-good + Stale; PA_GRAPH_MOCK
 * nur, wenn noch nie Live-Daten da waren oder nodes leer ist). Der frühere
 * statische Fokus (PA_GRAPH_MOCK_FOCUS_ID) entfällt — Fokus ist jetzt echt.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode } from "@/lib/api";
import { de } from "../i18n/de";
import { JARVIS_BRAIN_MOCKTAG } from "./mockContent";
import { formatGraphStand, usePaGraphView } from "./usePaGraph";

const t = de.jarvis;

/* ── Renderer-Konstanten (A4-Maße, Begründung im Kopfkommentar) ── */

export function nodeRadius(weight: number): number {
  return Math.round((1.5 + 13.5 * weight) * 10) / 10;
}
/** Ab diesem Gewicht Orb-Gradient statt simpler Farbfläche. */
export const ORB_WEIGHT = 0.45;
/** Ab diesem Gewicht zusätzlich der weiche Glow-Halo. */
export const GLOW_WEIGHT = 0.5;
/** Label-Schwelle + Cap: Top-Knoten je Cluster. */
export const LABEL_WEIGHT = 0.45;
export const LABELS_PER_CLUSTER = 5;
/** „big"-Label für die Schwergewichte (Mock: vision/Hermes-Infra/Jarvis). */
export const BIG_LABEL_WEIGHT = 0.7;

/** Live-Labels sind lang (Vault-Titel) — dezent kürzen, voller Text bleibt
 *  im Tooltip des Knotens. */
export const MAX_LABEL_CHARS = 30;

export function truncateLabel(label: string): string {
  return label.length > MAX_LABEL_CHARS ? `${label.slice(0, MAX_LABEL_CHARS - 1)}…` : label;
}

/** Cluster-IDs, für die die handgemischten A4-Gradients existieren. */
const A4_GRADIENT_IDS = new Set([
  "canon",
  "projekte",
  "agenten",
  "skills",
  "memories",
  "receipts",
  "archiv",
]);

const EDGE_COLOR = "#7fa8dc";

/** Kanten-Staffelung nach dem leichteren Endpunkt (A4-Näherung). */
export function edgeTier(weight: number): { opacity: number; width: number } {
  if (weight >= 0.55) return { opacity: 0.34, width: 1.2 };
  if (weight >= 0.35) return { opacity: 0.24, width: 1.0 };
  return { opacity: 0.17, width: 0.8 };
}

/** A4-Kurven: Kontrollpunkt leicht senkrecht zur Kante versetzt. */
export function edgePath(x1: number, y1: number, x2: number, y2: number): string {
  const mx = (x1 + x2) / 2 + 0.09 * (y1 - y2);
  const my = (y1 + y2) / 2 + 0.09 * (x2 - x1);
  return `M${x1} ${y1} Q${mx.toFixed(1)} ${my.toFixed(1)} ${x2} ${y2}`;
}

/**
 * Navigation aus dem Graphen: nur echte Dashboard-/API-Pfade sind
 * navigierbar; vault://, memory:// & Co. sind semantische Anzeige-Refs.
 * Gibt true zurück, wenn navigiert wurde (für Tests und Tap-Logik).
 */
export function openGraphRef(
  href: string | undefined,
  nav: { navigate: (path: string) => void; assign: (url: string) => void },
): boolean {
  if (!href) return false;
  if (href.startsWith("/control/")) {
    nav.navigate(href);
    return true;
  }
  if (href.startsWith("/api/")) {
    nav.assign(href);
    return true;
  }
  return false;
}

interface GraphModel {
  byId: Map<string, PaGraphNode>;
  clusterMeta: Map<string, PaGraphCluster>;
  /** Aufsteigend nach Gewicht — Schwergewichte malen obenauf. */
  sortedNodes: PaGraphNode[];
  auras: { clusterId: string; cx: number; cy: number; r: number }[];
  heroId: string | null;
  edges: { key: string; d: string; from: string; to: string; tierWeight: number }[];
  labels: Map<string, "big" | "normal">;
}

/** Einfacher Schwerpunkt je Cluster für die Auren (Mittelwert der Knoten,
 *  Radius = weitester Knoten + 70, geclamped auf die A4-Bandbreite 100–260). */
function buildModel(
  clusters: PaGraphCluster[],
  nodes: PaGraphNode[],
  edges: PaGraphEdge[],
  focusId: string | null,
): GraphModel {
  const clusterMeta = new Map(clusters.map((c) => [c.id, c]));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const sortedNodes = [...nodes].sort((a, b) => a.weight - b.weight);

  const auras: GraphModel["auras"] = [];
  const sums = new Map<string, { sx: number; sy: number; n: number }>();
  for (const n of nodes) {
    const s = sums.get(n.cluster) ?? { sx: 0, sy: 0, n: 0 };
    s.sx += n.x;
    s.sy += n.y;
    s.n += 1;
    sums.set(n.cluster, s);
  }
  for (const [clusterId, s] of sums) {
    if (!A4_GRADIENT_IDS.has(clusterId)) continue;
    const cx = s.sx / s.n;
    const cy = s.sy / s.n;
    let maxDist = 0;
    for (const n of nodes) {
      if (n.cluster !== clusterId) continue;
      maxDist = Math.max(maxDist, Math.hypot(n.x - cx, n.y - cy));
    }
    auras.push({ clusterId, cx, cy, r: Math.min(260, Math.max(100, maxDist + 70)) });
  }

  const heroId = sortedNodes.length > 0 ? sortedNodes[sortedNodes.length - 1].id : null;

  const edgeModels: GraphModel["edges"] = [];
  edges.forEach((e, i) => {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) return; // Kontrakt sagt beide Endpunkte vorhanden; defensiv.
    edgeModels.push({
      key: `${e.from}->${e.to}#${i}`,
      d: edgePath(a.x, a.y, b.x, b.y),
      from: e.from,
      to: e.to,
      tierWeight: Math.min(a.weight, b.weight),
    });
  });

  const labels = new Map<string, "big" | "normal">();
  const labelCandidates = new Map<string, PaGraphNode[]>();
  for (const n of nodes) {
    if (n.label == null || n.weight < LABEL_WEIGHT) continue;
    const list = labelCandidates.get(n.cluster) ?? [];
    list.push(n);
    labelCandidates.set(n.cluster, list);
  }
  for (const list of labelCandidates.values()) {
    list.sort((a, b) => b.weight - a.weight);
    for (const n of list.slice(0, LABELS_PER_CLUSTER)) {
      labels.set(n.id, n.weight >= BIG_LABEL_WEIGHT ? "big" : "normal");
    }
  }
  // Der fokussierte Knoten ist immer gelabelt, auch unter der Schwelle.
  if (focusId) {
    const focusNode = byId.get(focusId);
    if (focusNode?.label != null && !labels.has(focusId)) labels.set(focusId, "normal");
  }

  return { byId, clusterMeta, sortedNodes, auras, heroId, edges: edgeModels, labels };
}

/** Handgemischte A4-Gradients/Filter (IDs mit jv-Prefix, unverändert). */
function GraphDefs() {
  return (
    <defs>
      <filter id="jv-blur" x="-60%" y="-60%" width="220%" height="220%">
        <feGaussianBlur stdDeviation="5" />
      </filter>
      <radialGradient id="jv-fog" cx="50%" cy="45%" r="60%">
        <stop offset="0%" stopColor="#38d8ff" stopOpacity=".06" />
        <stop offset="100%" stopColor="#000" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-canon" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#bff3ff" />
        <stop offset="42%" stopColor="#38d8ff" />
        <stop offset="100%" stopColor="#1a647a" />
      </radialGradient>
      <radialGradient id="jv-neb-canon">
        <stop offset="0%" stopColor="#38d8ff" stopOpacity=".13" />
        <stop offset="55%" stopColor="#38d8ff" stopOpacity=".05" />
        <stop offset="100%" stopColor="#38d8ff" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-projekte" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#c1f4de" />
        <stop offset="42%" stopColor="#3ddc97" />
        <stop offset="100%" stopColor="#1d664c" />
      </radialGradient>
      <radialGradient id="jv-neb-projekte">
        <stop offset="0%" stopColor="#3ddc97" stopOpacity=".13" />
        <stop offset="55%" stopColor="#3ddc97" stopOpacity=".05" />
        <stop offset="100%" stopColor="#3ddc97" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-agenten" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#ffe7c4" />
        <stop offset="42%" stopColor="#ffb347" />
        <stop offset="100%" stopColor="#745428" />
      </radialGradient>
      <radialGradient id="jv-neb-agenten">
        <stop offset="0%" stopColor="#ffb347" stopOpacity=".13" />
        <stop offset="55%" stopColor="#ffb347" stopOpacity=".05" />
        <stop offset="100%" stopColor="#ffb347" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-skills" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#cbdaff" />
        <stop offset="42%" stopColor="#5b8cff" />
        <stop offset="100%" stopColor="#2a427a" />
      </radialGradient>
      <radialGradient id="jv-neb-skills">
        <stop offset="0%" stopColor="#5b8cff" stopOpacity=".13" />
        <stop offset="55%" stopColor="#5b8cff" stopOpacity=".05" />
        <stop offset="100%" stopColor="#5b8cff" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-memories" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#e8daff" />
        <stop offset="42%" stopColor="#b78cff" />
        <stop offset="100%" stopColor="#53427a" />
      </radialGradient>
      <radialGradient id="jv-neb-memories">
        <stop offset="0%" stopColor="#b78cff" stopOpacity=".13" />
        <stop offset="55%" stopColor="#b78cff" stopOpacity=".05" />
        <stop offset="100%" stopColor="#b78cff" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-receipts" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#ffd4e8" />
        <stop offset="42%" stopColor="#ff7ab8" />
        <stop offset="100%" stopColor="#743a5a" />
      </radialGradient>
      <radialGradient id="jv-neb-receipts">
        <stop offset="0%" stopColor="#ff7ab8" stopOpacity=".13" />
        <stop offset="55%" stopColor="#ff7ab8" stopOpacity=".05" />
        <stop offset="100%" stopColor="#ff7ab8" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="jv-orb-archiv" cx="38%" cy="30%" r="78%">
        <stop offset="0%" stopColor="#cad1db" />
        <stop offset="42%" stopColor="#5a6f8f" />
        <stop offset="100%" stopColor="#2a3548" />
      </radialGradient>
      <radialGradient id="jv-neb-archiv">
        <stop offset="0%" stopColor="#5a6f8f" stopOpacity=".13" />
        <stop offset="55%" stopColor="#5a6f8f" stopOpacity=".05" />
        <stop offset="100%" stopColor="#5a6f8f" stopOpacity="0" />
      </radialGradient>
    </defs>
  );
}

/** Sternenstaub — statischer A4-Rahmen (ruhig, Constellation-Tiefe). */
function StarDust() {
  return (
    <g>
      <circle cx="1216" cy="452" r="1.3" fill="#cfe4ff" opacity="0.12" />
      <circle cx="934" cy="299" r="0.8" fill="#cfe4ff" opacity="0.29" />
      <circle cx="191" cy="241" r="0.6" fill="#cfe4ff" opacity="0.12" />
      <circle cx="237" cy="43" r="1.2" fill="#cfe4ff" opacity="0.14" />
      <circle cx="339" cy="290" r="1.1" fill="#cfe4ff" opacity="0.19" />
      <circle cx="227" cy="676" r="1.3" fill="#cfe4ff" opacity="0.24" />
      <circle cx="779" cy="412" r="0.9" fill="#cfe4ff" opacity="0.14" />
      <circle cx="646" cy="467" r="0.6" fill="#cfe4ff" opacity="0.11" />
      <circle cx="718" cy="52" r="0.8" fill="#cfe4ff" opacity="0.26" />
      <circle cx="700" cy="60" r="0.7" fill="#cfe4ff" opacity="0.10" />
      <circle cx="75" cy="601" r="0.8" fill="#cfe4ff" opacity="0.29" />
      <circle cx="234" cy="350" r="1.0" fill="#cfe4ff" opacity="0.22" />
      <circle cx="308" cy="560" r="0.9" fill="#cfe4ff" opacity="0.27" />
      <circle cx="165" cy="619" r="0.8" fill="#cfe4ff" opacity="0.16" />
      <circle cx="1081" cy="402" r="1.2" fill="#cfe4ff" opacity="0.12" />
      <circle cx="1069" cy="300" r="0.7" fill="#cfe4ff" opacity="0.12" />
      <circle cx="256" cy="377" r="0.9" fill="#cfe4ff" opacity="0.14" />
      <circle cx="652" cy="213" r="0.8" fill="#cfe4ff" opacity="0.20" />
      <circle cx="580" cy="397" r="0.9" fill="#cfe4ff" opacity="0.25" />
      <circle cx="270" cy="316" r="1.0" fill="#cfe4ff" opacity="0.23" />
      <circle cx="56" cy="26" r="0.5" fill="#cfe4ff" opacity="0.26" />
      <circle cx="449" cy="210" r="0.7" fill="#cfe4ff" opacity="0.15" />
      <circle cx="204" cy="24" r="1.0" fill="#cfe4ff" opacity="0.13" />
      <circle cx="859" cy="193" r="0.6" fill="#cfe4ff" opacity="0.10" />
      <circle cx="911" cy="111" r="0.6" fill="#cfe4ff" opacity="0.30" />
      <circle cx="348" cy="232" r="0.6" fill="#cfe4ff" opacity="0.16" />
      <circle cx="399" cy="648" r="0.8" fill="#cfe4ff" opacity="0.29" />
      <circle cx="520" cy="139" r="0.5" fill="#cfe4ff" opacity="0.17" />
      <circle cx="833" cy="290" r="1.1" fill="#cfe4ff" opacity="0.11" />
      <circle cx="204" cy="52" r="0.7" fill="#cfe4ff" opacity="0.08" />
      <circle cx="67" cy="339" r="1.1" fill="#cfe4ff" opacity="0.08" />
      <circle cx="161" cy="206" r="0.9" fill="#cfe4ff" opacity="0.15" />
      <circle cx="1209" cy="45" r="1.0" fill="#cfe4ff" opacity="0.21" />
      <circle cx="747" cy="178" r="0.7" fill="#cfe4ff" opacity="0.08" />
      <circle cx="935" cy="269" r="0.6" fill="#cfe4ff" opacity="0.16" />
      <circle cx="190" cy="164" r="1.3" fill="#cfe4ff" opacity="0.12" />
      <circle cx="923" cy="171" r="0.5" fill="#cfe4ff" opacity="0.24" />
      <circle cx="824" cy="309" r="1.2" fill="#cfe4ff" opacity="0.30" />
      <circle cx="924" cy="282" r="1.3" fill="#cfe4ff" opacity="0.29" />
      <circle cx="164" cy="729" r="0.9" fill="#cfe4ff" opacity="0.22" />
      <circle cx="578" cy="441" r="1.1" fill="#cfe4ff" opacity="0.27" />
      <circle cx="688" cy="154" r="1.0" fill="#cfe4ff" opacity="0.27" />
      <circle cx="755" cy="422" r="0.8" fill="#cfe4ff" opacity="0.08" />
      <circle cx="1112" cy="694" r="1.1" fill="#cfe4ff" opacity="0.14" />
      <circle cx="986" cy="264" r="1.1" fill="#cfe4ff" opacity="0.12" />
      <circle cx="115" cy="598" r="1.1" fill="#cfe4ff" opacity="0.10" />
      <circle cx="1026" cy="605" r="1.0" fill="#cfe4ff" opacity="0.28" />
      <circle cx="255" cy="557" r="0.7" fill="#cfe4ff" opacity="0.22" />
      <circle cx="1219" cy="363" r="0.7" fill="#cfe4ff" opacity="0.20" />
      <circle cx="726" cy="263" r="1.1" fill="#cfe4ff" opacity="0.27" />
      <circle cx="630" cy="319" r="0.6" fill="#cfe4ff" opacity="0.17" />
      <circle cx="907" cy="495" r="1.1" fill="#cfe4ff" opacity="0.20" />
      <circle cx="1221" cy="556" r="0.5" fill="#cfe4ff" opacity="0.21" />
      <circle cx="1102" cy="372" r="0.8" fill="#cfe4ff" opacity="0.09" />
      <circle cx="710" cy="553" r="1.0" fill="#cfe4ff" opacity="0.22" />
    </g>
  );
}

export function JarvisGraph() {
  const view = usePaGraphView();
  const { graph } = view;
  const [focusId, setFocusId] = useState<string | null>(null);
  const navigate = useNavigate();

  const model = useMemo(
    () => buildModel(graph.clusters, graph.nodes, graph.edges, focusId),
    [graph, focusId],
  );
  const focusNode = focusId ? model.byId.get(focusId) : undefined;
  const focusColor = focusNode
    ? (model.clusterMeta.get(focusNode.cluster)?.color ?? EDGE_COLOR)
    : EDGE_COLOR;

  // Teilquellen-Fehler dezent: Console (Tooltip hängt am Footer-Tag). Kein Panel.
  const sourceErrorKey = view.sourceErrors.map((e) => `${e.source}: ${e.error}`).join("\n");
  useEffect(() => {
    if (sourceErrorKey) console.warn(`[JarvisGraph] Teilquellen-Fehler:\n${sourceErrorKey}`);
  }, [sourceErrorKey]);

  const handleTap = (node: PaGraphNode) => {
    if (focusId === node.id) {
      // Erneuter Tap auf den fokussierten Knoten = öffnen (nur navigierbare hrefs).
      openGraphRef(node.href ?? node.ref, {
        navigate,
        assign: (url) => window.location.assign(url),
      });
    } else {
      setFocusId(node.id);
    }
  };

  const ariaLabel = view.isLive
    ? t.graphAriaLive(graph.nodes.length) + (view.isStale ? t.graphAriaStaleSuffix : "")
    : t.graphAriaMock;

  return (
    <svg
      className="jv-brain"
      viewBox="0 0 1280 820"
      preserveAspectRatio="xMidYMid slice"
      aria-label={ariaLabel}
      role="img"
      onClick={() => setFocusId(null)}
    >
      <GraphDefs />
      <rect width="1280" height="820" fill="url(#jv-fog)" />
      <StarDust />
      {/* Cluster-Auren: Zentren aus dem Datenschwerpunkt (nicht hardcodiert) */}
      <g>
        {model.auras.map((aura) => (
          <circle
            key={aura.clusterId}
            cx={Math.round(aura.cx)}
            cy={Math.round(aura.cy)}
            r={Math.round(aura.r)}
            fill={`url(#jv-neb-${aura.clusterId})`}
          />
        ))}
        {/* Hero-Aura über dem gewichtigsten Knoten (A4-Centerpiece). */}
        {model.heroId != null &&
          (() => {
            const hero = model.byId.get(model.heroId);
            if (!hero || !A4_GRADIENT_IDS.has(hero.cluster)) return null;
            return <circle cx={hero.x} cy={hero.y} r="98" fill={`url(#jv-neb-${hero.cluster})`} />;
          })()}
      </g>
      {/* Kanten: ruhige Kurven, Stärke = leichteres Endpunkt-Gewicht */}
      <g>
        {model.edges.map((edge) => {
          const touching = focusNode != null && (edge.from === focusNode.id || edge.to === focusNode.id);
          const tier = edgeTier(edge.tierWeight);
          return (
            <path
              key={edge.key}
              data-edge={`${edge.from}->${edge.to}`}
              d={edge.d}
              fill="none"
              stroke={touching ? focusColor : EDGE_COLOR}
              strokeOpacity={touching ? 0.6 : tier.opacity}
              strokeWidth={touching ? 1.6 : tier.width}
            />
          );
        })}
      </g>
      {/* Knoten: Gewicht → Radius/Helligkeit; Hubs = Orb-Gradient + Glow */}
      <g>
        {model.sortedNodes.map((node) => {
          const color = model.clusterMeta.get(node.cluster)?.color ?? EDGE_COLOR;
          const r = nodeRadius(node.weight);
          const isFocus = focusNode?.id === node.id;
          const hasGlow = node.weight >= GLOW_WEIGHT;
          const orb = node.weight >= ORB_WEIGHT && A4_GRADIENT_IDS.has(node.cluster);
          const labeled = model.labels.has(node.id);
          const refText = node.href ?? node.ref;
          // Tooltip zeigt Label + Ref/Cluster — bewusst NIE exakt der
          // Label-Text (sonst kollidieren Text-Queries und Screenreader
          // lesen alles doppelt).
          const clusterLabel = model.clusterMeta.get(node.cluster)?.label ?? node.cluster;
          const tooltip = refText
            ? `${node.label ?? node.id}\n${refText}`
            : `${node.label ?? node.id} (${clusterLabel})`;
          return (
            <g
              key={node.id}
              data-node-id={node.id}
              role={labeled ? "button" : undefined}
              aria-label={labeled ? `${node.label} (${model.clusterMeta.get(node.cluster)?.label ?? node.cluster})` : undefined}
              aria-hidden={labeled ? undefined : true}
              tabIndex={labeled ? 0 : undefined}
              style={labeled ? { cursor: "pointer" } : undefined}
              onClick={(e) => {
                e.stopPropagation();
                handleTap(node);
              }}
              onKeyDown={
                labeled
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        e.stopPropagation();
                        handleTap(node);
                      }
                    }
                  : undefined
              }
            >
              <title>{tooltip}</title>
              {hasGlow && (
                <circle
                  className={model.heroId === node.id ? "jv-breathe" : undefined}
                  cx={node.x}
                  cy={node.y}
                  r={Math.round(r * 2.3)}
                  fill={color}
                  opacity=".26"
                  filter="url(#jv-blur)"
                />
              )}
              {isFocus && (
                <g data-focus-ring={node.id}>
                  <circle cx={node.x} cy={node.y} r={r + 7} fill="none" stroke={color} strokeOpacity=".85" strokeWidth="1.5" />
                  <circle cx={node.x} cy={node.y} r={r + 13} fill="none" stroke={color} strokeOpacity=".22" strokeWidth="1" />
                </g>
              )}
              <circle
                cx={node.x}
                cy={node.y}
                r={r}
                fill={orb ? `url(#jv-orb-${node.cluster})` : color}
                opacity={orb ? undefined : Math.round((0.55 + 0.35 * node.weight) * 100) / 100}
              />
            </g>
          );
        })}
      </g>
      {/* Labels: gewichtigste Knoten je Cluster (+ Fokus) — Staffelung wie A4 */}
      {model.sortedNodes.map((node) => {
        const kind = model.labels.get(node.id);
        if (!kind || node.label == null) return null;
        const r = nodeRadius(node.weight);
        const left = node.x > 1050;
        return (
          <text
            key={`lbl-${node.id}`}
            className={kind === "big" ? "maplabel big" : "maplabel"}
            x={left ? node.x - r - 7 : node.x + r + 7}
            y={node.y + 4}
            textAnchor={left ? "end" : undefined}
          >
            {truncateLabel(node.label)}
          </text>
        );
      })}
      {focusNode?.label != null && (
        <text
          className="maplabel focuslbl"
          x={focusNode.x}
          y={focusNode.y + nodeRadius(focusNode.weight) + 24}
          textAnchor="middle"
        >
          · FOKUS ·
        </text>
      )}
    </svg>
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
