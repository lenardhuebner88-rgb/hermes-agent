/**
 * JarvisShellView — die Jarvis-Zone auf /control/projekte (Sprint 1, Karte e;
 * Sprint 2, Karten S2.2/S2.4/S2.6/M3).
 *
 * Dunkles Command-Center-HUD nach dem Piet-freigegebenen A4-Mockup
 * (Design-Board c_8c6f034b): Estate-Graph als Vollbild-Canvas (S2.7: live an
 * /api/pa/graph — Zustands-Tag live vs. Mock-Fallback), schwebende Panels,
 * KI-Lage + Sparklines als statischer A4-Mock (S1; S4-Härtung: alle Statik-
 * Panels tragen denselben sichtbaren Mock-Tag wie der Graph-Fallback), Wartet-dezent an der
 * echten Entscheidungs-Inbox (S2.4: /api/pa/inbox — Expand zur Inbox-Ansicht
 * mit Approval-Cards für pa_action), PROJEKTE-Panel mit den echten
 * ProjectCards (S2.6 — gleiche Hooks/Ableitung wie die Klassik, Tap →
 * Klassik-Drilldown per Link), funktionale Frag-Leiste mit Bubble-Chat gegen
 * die LIVE-PA-Endpoints. M3: die Höhe des OfflineStaleBanner reist als
 * --jv-banner-h in die Stage-Höhe (Frag-Leiste clippt nicht mehr). S3.10:
 * AKTIVITÄT (Receipts+Commits) und SESSIONS (Spawn-Baum) als HUD-Strips im
 * Band zwischen PROJEKTE und Chat — der Expand öffnet je einen Overlay-
 * Drawer (Tabs/Filter-Chips), Lese- und Kill-Sheet kommen unverändert aus
 * der Klassik. S5-Design („JARVIS OS"): die S1-Mock-Panels (Brain/Filter/
 * KI-LAGE/Sparklines) stehen hinter einem HUD-Toggle (localStorage
 * hermes.jarvis.hud, Default aus; Mobile zeigt sie gar nicht), die Graph-
 * Ambience ist stärker gedimmt, und der Chat trägt Orb + Periphery-Zeile —
 * deren Tap öffnet den Aktivitaet-Drawer über das Window-Event
 * JARVIS_OPEN_AKTIVITAET_EVENT. Das alte S1-Emblem (rechts unten) ist mit S5
 * entfallen — einzige Engine-Wahl ist der Switcher im Orb-Header des Chats
 * (JarvisOrb), es gibt nur noch EINEN Orb pro Bildschirm.
 * Der bisherige Projekte-Tab bleibt als
 * /control/projekte-klassisch erreichbar (Fallback bis S2/S3 migrieren).
 *
 * Styles kommen ausschließlich aus ../jarvis.css (unter `.jv` gescopet,
 * lazy mit diesem Chunk geladen) — die einzige Route mit Ratchet-Ausnahme,
 * siehe DESIGN.md „Jarvis-Zone".
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import "../jarvis.css";
import { de } from "../i18n/de";
import { AktivitaetPanel } from "./AktivitaetPanel";
import { JARVIS_OPEN_AKTIVITAET_EVENT, JarvisChat } from "./JarvisChat";
import { JarvisGraph, JarvisGraphStatsTag, JarvisGraphTag } from "./JarvisGraph";
import { ProjektePanel } from "./ProjektePanel";
import { SessionsPanel } from "./SessionsPanel";
import { useOfflineBannerHeight } from "./useOfflineBannerHeight";
import { WartetPanel } from "./WartetPanel";
import {
  JARVIS_BRAIN_STATS,
  JARVIS_FILTER_ROWS,
  JARVIS_MOCK_TAG,
  JARVIS_NEWS_CRON,
  JARVIS_NEWS_ITEMS,
  JARVIS_SEARCH_HINT,
  JARVIS_SPARKS,
  JARVIS_TOP_HUBS,
} from "./mockContent";

const t = de.jarvis;

/** S4-Härtung: sichtbarer Mock-Tag an den statischen A4-Panels — dasselbe
 *  Label-Muster (.jv-mocktag) wie der Graph-Fallback in JarvisGraph, plus
 *  .jv-panelmock für die Pill-Optik am Panel-Titel. */
function MockTag() {
  return <span className="jv-mocktag jv-panelmock">{JARVIS_MOCK_TAG}</span>;
}

/** Welcher S3.10-Drawer offen ist (höchstens einer gleichzeitig — die
 *  Drawer teilen sich dieselbe Overlay-Zone mittig über dem Graphen).
 *  `?aktivitaet=open` / `?sessions=open` öffnen initial (Deep-Link/
 *  Screenshot-Naht wie ?inbox=open bei S2.4). */
type ShellPanel = "aktivitaet" | "sessions";

function initialOpenPanel(): ShellPanel | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  if (params.get("aktivitaet") === "open") return "aktivitaet";
  if (params.get("sessions") === "open") return "sessions";
  return null;
}

/** S5-Design („JARVIS OS"): die S1-Mock-Panels (Brain/Filter/KI-LAGE/
 *  Sparklines) stehen hinter einem HUD-Toggle — Default AUS (persistiert in
 *  localStorage), Mobile zeigt sie gar nicht (CSS). Der Mock-Tag aus der
 *  S4-Härtung bleibt am Code für den HUD-Modus. */
const HUD_STORAGE_KEY = "hermes.jarvis.hud";

function initialHud(): boolean {
  try {
    return window.localStorage.getItem(HUD_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function JarvisShellView() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  useOfflineBannerHeight(rootRef);
  const [openPanel, setOpenPanel] = useState<ShellPanel | null>(initialOpenPanel);
  const togglePanel = (panel: ShellPanel) =>
    setOpenPanel((current) => (current === panel ? null : panel));
  const [hud, setHud] = useState<boolean>(initialHud);
  const toggleHud = () =>
    setHud((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(HUD_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // Privatmodus/Quota: Toggle wirkt dann eben nur sitzungslokal.
      }
      return next;
    });

  // S5-Design: Tap auf die Periphery-Zeile im Chat öffnet den Aktivitaet-
  // Drawer (kleines Window-Event statt Prop-Bohrung durch die Shell).
  useEffect(() => {
    const onOpenAktivitaet = () => setOpenPanel("aktivitaet");
    window.addEventListener(JARVIS_OPEN_AKTIVITAET_EVENT, onOpenAktivitaet);
    return () => window.removeEventListener(JARVIS_OPEN_AKTIVITAET_EVENT, onOpenAktivitaet);
  }, []);

  return (
    <div className="jv" ref={rootRef}>
      <div className={hud ? "jv-stage" : "jv-stage jv-hud-off"}>
        <JarvisGraph />

        {/* ══ Links: Brain-Panel ══ */}
        <div className="jv-float jv-brainpanel">
          <h1>
            PIET-ESTATE <b>OS</b> <MockTag />
          </h1>
          <div className="jv-stats">
            {JARVIS_BRAIN_STATS}
            <JarvisGraphStatsTag />
          </div>
          <div className="jv-search">{JARVIS_SEARCH_HINT}</div>
          <div className="jv-hubs">
            <div className="jv-ptitle" style={{ marginBottom: 5 }}>
              TOP-HUBS
            </div>
            {JARVIS_TOP_HUBS.map((hub) => (
              <div className="jv-hub" key={hub.name}>
                <span className={`jv-d jv-tone-${hub.tone}`} aria-hidden="true" />
                <span className="jv-nm">{hub.name}</span>
                <span className="jv-n">{hub.count}</span>
              </div>
            ))}
          </div>
          <div className="jv-inspector">
            <b>Knoten antippen</b> → fokussiert ihn samt Verbindungen. <b>Erneut tippen</b> →
            Ziel öffnen (Tasks/Receipts); vault://- und memory://-Refs sind reine Anzeige. Jarvis
            nutzt denselben Graphen als Gedächtnis.
          </div>
          <Link className="jv-klassisch" to="/control/projekte-klassisch">
            {t.klassischLink}
          </Link>
        </div>

        {/* ══ Rechts oben: Filter ══ */}
        <div className="jv-float jv-filter">
          <div className="jv-ptitle">
            FILTER <MockTag />
          </div>
          {JARVIS_FILTER_ROWS.map((row) => (
            <div className="jv-frow" key={row.name}>
              <span className={`jv-d jv-tone-${row.tone}`} aria-hidden="true" />
              {row.name} <span className="jv-n">{row.count}</span>
            </div>
          ))}
        </div>

        {/* ══ Rechts: KI-LAGE (statischer A4-Mock, S1) ══ */}
        <div className="jv-float jv-news">
          <div className="jv-ptitle">
            KI-LAGE <MockTag /> <span className="jv-fresh">{JARVIS_NEWS_CRON}</span>
          </div>
          {JARVIS_NEWS_ITEMS.map((item) => (
            <div className={item.lead ? "jv-item jv-lead" : "jv-item"} key={item.text}>
              {item.text}
              <span className="jv-src">{item.source}</span>
            </div>
          ))}
        </div>

        {/* ══ Links unten: Wartet · dezent (echte Fragen) + System (Mock) ══ */}
        <div className="jv-float jv-quiet">
          <WartetPanel />
          <div className="jv-sys">
            {JARVIS_SPARKS.map((spark) => (
              <div className="jv-spark" key={spark.label}>
                <div className="jv-lb">
                  {spark.label} <b>{spark.value}</b>
                </div>
                <svg viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true">
                  <path d={spark.areaPath} className={`jv-sparkarea-${spark.tone}`} />
                  <path d={spark.linePath} className={`jv-sparkline-${spark.tone}`} />
                </svg>
              </div>
            ))}
            <span className="jv-sysmock">
              <MockTag />
            </span>
          </div>
        </div>

        {/* ══ Mitte oben: PROJEKTE (S2.6 — echte ProjectCards im A4-Look) ══ */}
        <ProjektePanel />

        {/* ══ Band unter PROJEKTE: AKTIVITÄT + SESSIONS (S3.10 — HUD-Strips,
            Expand öffnet den Overlay-Drawer; Daten/Sheets der Klassik) ══ */}
        <div className="jv-strips">
          <AktivitaetPanel
            open={openPanel === "aktivitaet"}
            onToggle={() => togglePanel("aktivitaet")}
          />
          <SessionsPanel
            open={openPanel === "sessions"}
            onToggle={() => togglePanel("sessions")}
          />
        </div>

        {/* ══ Graph-Zustands-Tag (Desktop; mobil: inline in .jv-stats) ══ */}
        <JarvisGraphTag />

        {/* ══ S5-Design: HUD-Toggle für die S1-Mock-Panels (Desktop; Default
            aus, persistiert — Mobile zeigt die Panels gar nicht) ══ */}
        <button
          type="button"
          className={hud ? "jv-hudtoggle jv-on" : "jv-hudtoggle"}
          aria-pressed={hud}
          aria-label={t.hudToggle}
          title={t.hudToggle}
          onClick={toggleHud}
        >
          HUD
        </button>

        {/* ══ Chat: Bubble-Verlauf + Frag-Leiste (LIVE PA-Endpoints) ══ */}
        <JarvisChat />
      </div>
    </div>
  );
}
