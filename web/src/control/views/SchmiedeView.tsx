import { usePromptForgeCatalog } from "../hooks/promptForge";
import type { Density } from "../hooks/useDensity";
import { TriangleAlert } from "lucide-react";
import { FleetEmptyState, FleetPanel } from "../components/leitstand";
import { Eyebrow } from "../components/primitives";
import { Konfigurator } from "./schmiede/Konfigurator";
import { Kanon } from "./schmiede/Kanon";

export function SchmiedeView(_props: { density?: Density }) {
  const { data, error, loading } = usePromptForgeCatalog();

  return (
    <div className="grid grid-cols-1 gap-4">
      <header>
        <Eyebrow>Prompt-Schmiede</Eyebrow>
        <h2 className="mt-1 font-display text-h2 font-semibold text-ink">Best-Practice-Prompts für Agent-Steuerbefehle</h2>
        <p className="mt-1 text-body text-ink-2">Konfigurieren → kopieren → in Claude Code / Codex einfügen. Kein Dispatch, nur Text.</p>
      </header>

      {loading && !data ? (
        <FleetPanel eyebrow="Lädt"><p className="text-ink-2 text-sm">Katalog wird geladen …</p></FleetPanel>
      ) : error && !data ? (
        <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span><strong>Katalog nicht erreichbar:</strong> {error}</span></div>
      ) : data ? (
        <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
          <Konfigurator catalog={data} />
          <Kanon catalog={data} />
        </div>
      ) : (
        <FleetPanel eyebrow="Leer"><FleetEmptyState title="Kein Katalog" desc="Die Antwort enthielt keine Daten." /></FleetPanel>
      )}
    </div>
  );
}
