import { usePromptForgeCatalog } from "../hooks/useControlData";
import type { Density } from "../hooks/useDensity";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { Konfigurator } from "./schmiede/Konfigurator";
import { Kanon } from "./schmiede/Kanon";

export function SchmiedeView(_props: { density?: Density }) {
  const { data, error, loading } = usePromptForgeCatalog();

  return (
    <div className="grid gap-4">
      <header>
        <p className="hc-eyebrow">Prompt-Schmiede</p>
        <h2 className="mt-1 text-xl font-semibold text-white">Best-Practice-Prompts für Agent-Steuerbefehle</h2>
        <p className="mt-1 hc-soft text-sm">Konfigurieren → kopieren → in Claude Code / Codex einfügen. Kein Dispatch, nur Text.</p>
      </header>

      {loading && !data ? (
        <FleetPanel eyebrow="Lädt"><p className="hc-soft text-sm">Katalog wird geladen …</p></FleetPanel>
      ) : error && !data ? (
        <FleetPanel eyebrow="Fehler"><FleetEmptyState title="Katalog nicht erreichbar" desc={error} /></FleetPanel>
      ) : data ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Konfigurator catalog={data} />
          <Kanon catalog={data} />
        </div>
      ) : (
        <FleetPanel eyebrow="Leer"><FleetEmptyState title="Kein Katalog" desc="Die Antwort enthielt keine Daten." /></FleetPanel>
      )}
    </div>
  );
}
