/**
 * PA-Graph Mock-Datensatz — 1:1-Port der `#pa-graph-mock`-Insel aus dem
 * Piet-freigegebenen A4-Mockup (Design-Board c_8c6f034b,
 * jarvis-variante-a4-brain-feinschliff.html). Der Estate-Graph auf
 * /control/projekte ist in Sprint 1 bewusst ein MOCK (Brief F11): diese
 * Struktur ist der austauschbare Datenkontrakt, ab S2.7 füllt
 * GET /api/pa/graph ihn mit echten Daten. Die SVG in JarvisGraph.tsx ist
 * die vom Mockup statisch gerenderte Vorschau genau dieses Datensatzes
 * (gleiche Knoten/Labels/Cluster-Farben — abgesichert über JarvisGraph.test).
 */

export interface PaGraphCluster {
  id: string;
  label: string;
  color: string;
}

export interface PaGraphNode {
  id: string;
  label: string | null;
  cluster: string;
  kind: string;
  weight: number;
  x: number;
  y: number;
  ref?: string;
}

export interface PaGraphEdge {
  from: string;
  to: string;
  kind: string;
}

export interface PaGraphMock {
  schema: string;
  source: string;
  layout: string;
  generated_at: string;
  refresh: { interval_s: number; on_error: string };
  clusters: PaGraphCluster[];
  nodes: PaGraphNode[];
  edges: PaGraphEdge[];
}

export const PA_GRAPH_MOCK: PaGraphMock = {
  schema: "pa-graph/v0-mock",
  source: "mock",
  layout: "precomputed-viewbox-1280x820",
  generated_at: "2026-07-19T12:00:00+02:00",
  refresh: { interval_s: 30, on_error: "keep-last-good + stale-badge" },
  clusters: [
    { id: "canon", label: "Canon", color: "#38d8ff" },
    { id: "projekte", label: "Projekte", color: "#3ddc97" },
    { id: "agenten", label: "Agenten", color: "#ffb347" },
    { id: "skills", label: "Skills", color: "#5b8cff" },
    { id: "memories", label: "Memories", color: "#b78cff" },
    { id: "receipts", label: "Receipts", color: "#ff7ab8" },
    { id: "archiv", label: "Archiv", color: "#5a6f8f" },
  ],
  nodes: [
    { id: "canon.vision", label: "vision", cluster: "canon", kind: "doc", weight: 1.0, x: 640, y: 400, ref: "vault://00-Canon/vision.md" },
    { id: "canon.conventions-gates", label: "conventions-gates", cluster: "canon", kind: "doc", weight: 0.55, x: 640, y: 245, ref: "vault://00-Canon/conventions-gates.md" },
    { id: "canon.planspec-taskgraph", label: "planspec-taskgraph", cluster: "canon", kind: "doc", weight: 0.37, x: 588, y: 182, ref: "vault://00-Canon/planspec-taskgraph.md" },
    { id: "canon.infra-topologie", label: "infra-topologie", cluster: "canon", kind: "doc", weight: 0.33, x: 700, y: 180, ref: "vault://00-Canon/infra-topology.md" },
    { id: "canon.c4", label: null, cluster: "canon", kind: "doc", weight: 0.27, x: 622, y: 130 },
    { id: "canon.c5", label: null, cluster: "canon", kind: "doc", weight: 0.23, x: 762, y: 140 },
    { id: "canon.c6", label: null, cluster: "canon", kind: "doc", weight: 0.27, x: 775, y: 172 },
    { id: "proj.hermes-infra", label: "Hermes-Infra", cluster: "projekte", kind: "project", weight: 0.73, x: 520, y: 520 },
    { id: "proj.health-track", label: "Health Track", cluster: "projekte", kind: "project", weight: 0.6, x: 448, y: 575 },
    { id: "proj.family-organizer", label: "Family Organizer", cluster: "projekte", kind: "project", weight: 0.53, x: 572, y: 592 },
    { id: "proj.diktat", label: "Diktat", cluster: "projekte", kind: "project", weight: 0.47, x: 440, y: 480 },
    { id: "proj.p5", label: null, cluster: "projekte", kind: "project", weight: 0.33, x: 392, y: 620 },
    { id: "proj.p6", label: null, cluster: "projekte", kind: "project", weight: 0.33, x: 600, y: 655 },
    { id: "proj.p7", label: null, cluster: "projekte", kind: "project", weight: 0.27, x: 345, y: 662 },
    { id: "proj.p8", label: null, cluster: "projekte", kind: "project", weight: 0.27, x: 640, y: 700 },
    { id: "proj.p9", label: null, cluster: "projekte", kind: "project", weight: 0.2, x: 298, y: 700 },
    { id: "ag.jarvis", label: "Jarvis", cluster: "agenten", kind: "agent", weight: 0.73, x: 795, y: 300 },
    { id: "ag.codex", label: "Codex", cluster: "agenten", kind: "agent", weight: 0.53, x: 868, y: 248 },
    { id: "ag.kimi", label: "Kimi K3", cluster: "agenten", kind: "agent", weight: 0.5, x: 852, y: 338 },
    { id: "ag.grok", label: "Grok", cluster: "agenten", kind: "agent", weight: 0.4, x: 742, y: 240 },
    { id: "ag.a5", label: null, cluster: "agenten", kind: "agent", weight: 0.33, x: 930, y: 212 },
    { id: "ag.a6", label: null, cluster: "agenten", kind: "agent", weight: 0.33, x: 922, y: 352 },
    { id: "ag.a7", label: null, cluster: "agenten", kind: "agent", weight: 0.23, x: 988, y: 180 },
    { id: "sk.hub", label: "Skills", cluster: "skills", kind: "skill", weight: 0.67, x: 500, y: 290 },
    { id: "sk.merge-deploy", label: "merge-deploy", cluster: "skills", kind: "skill", weight: 0.47, x: 415, y: 225 },
    { id: "sk.s3", label: null, cluster: "skills", kind: "skill", weight: 0.4, x: 452, y: 330 },
    { id: "sk.s4", label: null, cluster: "skills", kind: "skill", weight: 0.37, x: 560, y: 228 },
    { id: "sk.s5", label: null, cluster: "skills", kind: "skill", weight: 0.3, x: 352, y: 185 },
    { id: "sk.s6", label: null, cluster: "skills", kind: "skill", weight: 0.27, x: 378, y: 262 },
    { id: "sk.s7", label: null, cluster: "skills", kind: "skill", weight: 0.2, x: 300, y: 152 },
    { id: "mem.hub", label: "Memories", cluster: "memories", kind: "memory", weight: 0.6, x: 905, y: 415 },
    { id: "mem.m2", label: null, cluster: "memories", kind: "memory", weight: 0.43, x: 975, y: 392 },
    { id: "mem.r47", label: "jarvis-roadmap R47", cluster: "memories", kind: "memory", weight: 0.4, x: 962, y: 462 },
    { id: "mem.m4", label: null, cluster: "memories", kind: "memory", weight: 0.33, x: 1042, y: 368 },
    { id: "mem.m5", label: null, cluster: "memories", kind: "memory", weight: 0.3, x: 1030, y: 498 },
    { id: "mem.m6", label: null, cluster: "memories", kind: "memory", weight: 0.23, x: 1095, y: 330 },
    { id: "mem.m7", label: null, cluster: "memories", kind: "memory", weight: 0.2, x: 1080, y: 545 },
    { id: "rc.hub", label: "Receipts", cluster: "receipts", kind: "receipt", weight: 0.6, x: 780, y: 520 },
    { id: "rc.r2", label: null, cluster: "receipts", kind: "receipt", weight: 0.43, x: 845, y: 572 },
    { id: "rc.r3", label: null, cluster: "receipts", kind: "receipt", weight: 0.37, x: 722, y: 590 },
    { id: "rc.r4", label: null, cluster: "receipts", kind: "receipt", weight: 0.3, x: 905, y: 612 },
    { id: "rc.r5", label: null, cluster: "receipts", kind: "receipt", weight: 0.23, x: 700, y: 655 },
    { id: "rc.r6", label: null, cluster: "receipts", kind: "receipt", weight: 0.2, x: 958, y: 650 },
    { id: "arc.a1", label: null, cluster: "archiv", kind: "archive", weight: 0.33, x: 372, y: 452 },
    { id: "arc.a2", label: null, cluster: "archiv", kind: "archive", weight: 0.27, x: 310, y: 425 },
    { id: "arc.a3", label: null, cluster: "archiv", kind: "archive", weight: 0.2, x: 262, y: 392 },
  ],
  edges: [
    { from: "canon.vision", to: "sk.hub", kind: "link" },
    { from: "canon.vision", to: "ag.jarvis", kind: "link" },
    { from: "canon.vision", to: "proj.hermes-infra", kind: "link" },
    { from: "canon.vision", to: "rc.hub", kind: "link" },
    { from: "canon.vision", to: "canon.conventions-gates", kind: "link" },
    { from: "canon.vision", to: "mem.hub", kind: "link" },
    { from: "sk.hub", to: "sk.merge-deploy", kind: "link" },
    { from: "sk.hub", to: "sk.s3", kind: "link" },
    { from: "sk.hub", to: "sk.s4", kind: "link" },
    { from: "sk.merge-deploy", to: "sk.s5", kind: "link" },
    { from: "sk.merge-deploy", to: "sk.s6", kind: "link" },
    { from: "ag.jarvis", to: "ag.codex", kind: "link" },
    { from: "ag.jarvis", to: "ag.kimi", kind: "link" },
    { from: "ag.jarvis", to: "ag.grok", kind: "link" },
    { from: "ag.codex", to: "ag.a5", kind: "link" },
    { from: "ag.kimi", to: "ag.a6", kind: "link" },
    { from: "proj.hermes-infra", to: "proj.health-track", kind: "link" },
    { from: "proj.hermes-infra", to: "proj.family-organizer", kind: "link" },
    { from: "proj.hermes-infra", to: "proj.diktat", kind: "link" },
    { from: "proj.health-track", to: "proj.p5", kind: "link" },
    { from: "proj.family-organizer", to: "proj.p6", kind: "link" },
    { from: "rc.hub", to: "rc.r2", kind: "link" },
    { from: "rc.hub", to: "rc.r3", kind: "link" },
    { from: "rc.r2", to: "rc.r4", kind: "link" },
    { from: "canon.conventions-gates", to: "canon.planspec-taskgraph", kind: "link" },
    { from: "canon.conventions-gates", to: "canon.infra-topologie", kind: "link" },
    { from: "canon.planspec-taskgraph", to: "canon.c4", kind: "link" },
    { from: "mem.hub", to: "mem.m2", kind: "link" },
    { from: "mem.hub", to: "mem.r47", kind: "link" },
    { from: "mem.m2", to: "mem.m4", kind: "link" },
    { from: "mem.r47", to: "mem.m5", kind: "link" },
    { from: "mem.m4", to: "mem.m6", kind: "link" },
    { from: "proj.diktat", to: "arc.a1", kind: "link" },
    { from: "arc.a1", to: "arc.a2", kind: "link" },
    { from: "ag.grok", to: "canon.c6", kind: "link" },
    { from: "canon.infra-topologie", to: "canon.c5", kind: "link" },
    { from: "proj.p5", to: "proj.p7", kind: "link" },
    { from: "proj.p6", to: "proj.p8", kind: "link" },
    { from: "rc.r3", to: "rc.r5", kind: "link" },
    { from: "arc.a2", to: "arc.a3", kind: "link" },
    { from: "sk.s5", to: "sk.s7", kind: "link" },
    { from: "ag.a5", to: "ag.a7", kind: "link" },
    { from: "mem.m5", to: "mem.m7", kind: "link" },
    { from: "rc.r4", to: "rc.r6", kind: "link" },
    { from: "proj.p7", to: "proj.p9", kind: "link" },
    { from: "canon.vision", to: "proj.p8", kind: "link" },
    { from: "sk.hub", to: "canon.conventions-gates", kind: "link" },
    { from: "ag.jarvis", to: "mem.hub", kind: "link" },
  ],
};

/** Fokus-Knoten des A4-Mockups: Hermes-Infra (Ring + aktive Kanten + FOKUS-Tag). */
export const PA_GRAPH_MOCK_FOCUS_ID = "proj.hermes-infra";
