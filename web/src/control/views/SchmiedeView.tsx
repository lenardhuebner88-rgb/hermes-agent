import { usePromptForgeCatalog } from "../hooks/promptForge";
import type { Density } from "../hooks/useDensity";
import { TriangleAlert } from "lucide-react";
import { FleetEmptyState, FleetPanel, ViewHeader } from "../components/leitstand";
import { Konfigurator } from "./schmiede/Konfigurator";
import { Kanon } from "./schmiede/Kanon";

export function SchmiedeView(_props: { density?: Density }) {
  const { data, error, loading } = usePromptForgeCatalog();

  return (
    <div className="grid grid-cols-1 gap-4">
      <ViewHeader
        eyebrow="Prompt-Schmiede"
        title="Best-Practice-Prompts für Agent-Steuerbefehle"
        description="Konfigurieren → kopieren → in Claude Code / Codex einfügen. Kein Dispatch, nur Text."
      />

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
