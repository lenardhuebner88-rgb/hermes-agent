import { useEffect, useId, useState } from "react";
import { fetchJSON } from "@/lib/api";

// Phase B (Programm 3): wiederverwendbarer Modell-Picker — das Datalist-Muster
// aus der LanesView (F1) als Komponente gehoben. Freitext bleibt erlaubt;
// Vorschläge = statischer Katalog + live default_models aus GET /lanes
// (profiles[] kommt dynamisch aus den Profil-Configs).
const MODEL_SUGGESTIONS = [
  "claude-fable-5",
  "claude-opus-4-8",
  "claude-sonnet-4-6",
  "claude-haiku-4-5",
  "gpt-5.5",
  "gpt-5.4",
  "kimi-for-coding",
  "kimi-k2.6",
  "qwen3.7-max",
];

interface LanesCatalogProfile {
  name: string;
  worker_runtime?: string | null;
  default_model?: string | null;
}

interface LanesCatalogResponse {
  profiles?: LanesCatalogProfile[];
}

/** Live-Modelle aus den Profil-Configs, dedupliziert gegen den Katalog. */
function mergeSuggestions(
  base: string[], profiles: LanesCatalogProfile[],
): string[] {
  const seen = new Set(base);
  const merged = [...base];
  for (const p of profiles) {
    const model = (p.default_model ?? "").trim();
    if (model && !seen.has(model)) {
      seen.add(model);
      merged.push(model);
    }
  }
  return merged;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** aria-label / placeholder; Default „Modell". */
  label?: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

export function ModelPicker({ value, onChange, label, placeholder, disabled, className }: Props) {
  const datalistId = useId();
  const [suggestions, setSuggestions] = useState<string[]>(MODEL_SUGGESTIONS);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await fetchJSON<LanesCatalogResponse>("/api/plugins/kanban/lanes");
        if (!cancelled && Array.isArray(data.profiles)) {
          setSuggestions(mergeSuggestions(MODEL_SUGGESTIONS, data.profiles));
        }
      } catch {
        // Katalog ist nur Komfort — der statische Vorschlagssatz reicht.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  return (
    <>
      <input
        type="text"
        value={value}
        list={datalistId}
        aria-label={label ?? "Modell"}
        placeholder={placeholder ?? label ?? "Modell"}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={
          className ??
          "hc-mono w-48 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs text-white"
        }
      />
      <datalist id={datalistId}>
        {suggestions.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
    </>
  );
}
