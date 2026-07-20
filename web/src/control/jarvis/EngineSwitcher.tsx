/**
 * EngineSwitcher — S2.2 Modell-Switcher, seit S5-Design im Orb-Header des
 * Chats (JarvisOrb; vorher im Shell-Emblem, das mit S5 entfallen ist).
 *
 * Ersetzt das statische S1-Platzhalter-Badge („◆ GPT-5.6-SOL ▾"): Optionen
 * kommen aus dem echten Roster (GET /api/pa/engines, usePaEngines — gleicher
 * pollingStore-Key wie der Chat, kein zweiter Fetch). Die Wahl gilt für den
 * NÄCHSTEN Turn: sie landet im lokalen engineSelection-Store, den usePaChat
 * im send-Pfad liest (engine+model im POST /api/pa/message).
 *
 * Native <select> im A4-Pill-Look (Tastatur/Screenreader/Mobile-Picker
 * gratis). Solange das Roster nicht geladen ist (oder fehlschlägt), bleibt
 * das statische Badge als Fallback — kein Crash, kein leeres Dropdown.
 */
import { de } from "../i18n/de";
import {
  effectiveEngine,
  findEngineSpec,
  modelLabel,
  setEngineChoice,
  useEngineChoice,
  usePaEngines,
} from "./engineSelection";
import { JARVIS_EMBLEM_MODEL } from "./mockContent";

const t = de.jarvis;

export function EngineSwitcher() {
  const roster = usePaEngines();
  const choice = useEngineChoice();
  const engines = roster.data?.engines ?? null;

  if (!engines || engines.length === 0) {
    return <div className="jv-model">{JARVIS_EMBLEM_MODEL}</div>;
  }

  // Wahl gegen das aktuelle Roster validieren (Engine/Modell könnte bei einem
  // Deploy wegfallen) — ungültig → effektiv bleibt der Server-Default.
  const validChoice =
    choice && findEngineSpec(roster.data, choice.engine)?.models.includes(choice.model)
      ? choice
      : null;
  const engine = effectiveEngine(validChoice, roster.data);
  const model =
    validChoice?.model ?? findEngineSpec(roster.data, engine)?.default_model ?? "";
  const value = `${engine}:${model}`;

  return (
    <label className="jv-model jv-switch" title={t.switcherTitle}>
      <span aria-hidden="true">◆&nbsp;</span>
      <select
        className="jv-sw-select"
        aria-label={t.switcherLabel}
        value={value}
        onChange={(event) => {
          const [nextEngine, ...rest] = event.target.value.split(":");
          setEngineChoice({ engine: nextEngine, model: rest.join(":") });
        }}
      >
        {engines.flatMap((spec) =>
          spec.models.map((specModel) => {
            const key = `${spec.engine}:${specModel}`;
            return (
              <option key={key} value={key}>
                {modelLabel(specModel)}
              </option>
            );
          }),
        )}
      </select>
      <span aria-hidden="true">&nbsp;▾</span>
    </label>
  );
}
