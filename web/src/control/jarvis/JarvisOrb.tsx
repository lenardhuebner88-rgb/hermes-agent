/**
 * JarvisOrb — S5-Design („JARVIS OS"): das lebendige Emblem ÜBER dem
 * Gespräch. Der Orb ist die Identitätsschicht der neuen Zone: reine
 * CSS-Animation (Ringe + Core, Zustand über `.jv-orb--<state>`), keine neue
 * Dependency. Die Engine-Wahl bleibt am Orb — die Labelzeile darunter ist der
 * bestehende EngineSwitcher (Roster /api/pa/engines); ein Tap auf den Orb
 * fokussiert dessen Select direkt (kein Umweg über das Shell-Emblem).
 *
 * Zustände: idle (ruhiger Puls) · listening (Mic) · thinking (Turn läuft) ·
 * speaking (TTS) · error — das Mapping aus den Chat-Hooks macht JarvisChat.
 */
import { useRef } from "react";

import { de } from "../i18n/de";
import { EngineSwitcher } from "./EngineSwitcher";

const t = de.jarvis;

export type JarvisOrbState = "idle" | "listening" | "thinking" | "speaking" | "error";

const STATE_LABEL: Record<JarvisOrbState, string> = {
  idle: t.orbStateIdle,
  listening: t.orbStateListening,
  thinking: t.orbStateThinking,
  speaking: t.orbStateSpeaking,
  error: t.orbStateError,
};

export function JarvisOrb({
  state,
  engineLabel,
  onEngineClick,
}: {
  state: JarvisOrbState;
  /** Anzeigename des effektiven Modells (aria-Text des Orbs). */
  engineLabel: string;
  /** Zusatz-Callback nach dem Orb-Tap (Select ist dann bereits fokussiert). */
  onEngineClick?: () => void;
}) {
  const switchRef = useRef<HTMLDivElement | null>(null);
  const onOrbClick = () => {
    // Tap auf den Orb = Engine-Wahl: den Switcher-Select direkt fokussieren.
    switchRef.current?.querySelector("select")?.focus();
    onEngineClick?.();
  };
  return (
    <div className={`jv-orbwrap jv-orbwrap--${state}`}>
      <button
        type="button"
        className={`jv-orb jv-orb--${state}`}
        aria-label={t.orbAria(STATE_LABEL[state], engineLabel)}
        title={STATE_LABEL[state]}
        onClick={onOrbClick}
      >
        <span className="jv-orb-r" aria-hidden="true" />
        <span className="jv-orb-r jv-orb-r2" aria-hidden="true" />
        <span className="jv-orb-core" aria-hidden="true" />
      </button>
      <span className="jv-orbstate">{STATE_LABEL[state]}</span>
      <div className="jv-orbswitch" ref={switchRef}>
        <EngineSwitcher />
      </div>
    </div>
  );
}
