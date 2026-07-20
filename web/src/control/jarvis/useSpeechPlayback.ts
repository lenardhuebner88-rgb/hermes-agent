/**
 * useSpeechPlayback — Vorlese-Hook des Jarvis-Chats (S3.6), Port des
 * Desktop-Musters apps/desktop/src/lib/voice-playback.ts (playSpeechText)
 * auf den Browser: POST /api/audio/speak → `new Audio(data_url)`.
 *
 * Vom Vorbild übernommen:
 *  - Stall-Timeout (15 s) gegen hängende Provider-Audios, die weder
 *    `playing`/`ended` noch `error` feuern — rearms auf jedem timeupdate,
 *    legitime lange Sprache wird nie abgeschnitten.
 *  - sequence-Zähler gegen überlappende Play-Requests: ein neues play() oder
 *    stop() invalidiert das laufende sofort.
 *
 * Fehler sind best-effort und werden nie in den Chat geworfen — sie landen
 * als `speakError`-State für optionales UI. Der `enabled`-Toggle persistiert
 * in localStorage; Aus-Schalten stoppt laufende Wiedergabe sofort.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { de } from "../i18n/de";

const t = de.jarvis;

/** localStorage-Key des Vorlese-Toggles. */
export const SPEAK_ENABLED_STORAGE_KEY = "jarvis.speak.enabled";

/** Wie im Desktop-Vorbild: Playback ohne Fortschritt gilt nach 15 s als hängend. */
const PLAYBACK_STALL_MS = 15_000;

function readEnabled(): boolean {
  try {
    return window.localStorage.getItem(SPEAK_ENABLED_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function useSpeechPlayback(): {
  play: (text: string) => Promise<void>;
  stop: () => void;
  enabled: boolean;
  setEnabled: (next: boolean) => void;
  speakError: string | null;
} {
  const [enabled, setEnabledState] = useState<boolean>(readEnabled);
  const [speakError, setSpeakError] = useState<string | null>(null);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const sequenceRef = useRef(0);
  /** Löst das hängende Play-Promise eines gestoppten Audios sofort auf
   *  (Vorbild currentStop) — sonst bliebe es bis zum Stall-Timeout offen. */
  const stopPlaybackRef = useRef<(() => void) | null>(null);

  const stop = useCallback(() => {
    sequenceRef.current += 1;
    stopPlaybackRef.current?.();
    stopPlaybackRef.current = null;
    const audio = audioRef.current;
    audioRef.current = null;
    if (audio) {
      audio.pause();
      audio.src = "";
    }
  }, []);

  // Laufende Wiedergabe beim Unmount stoppen.
  useEffect(() => stop, [stop]);

  const setEnabled = useCallback(
    (next: boolean) => {
      setEnabledState(next);
      try {
        window.localStorage.setItem(SPEAK_ENABLED_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // Privatmodus/Quota: Toggle wirkt dann eben nur sitzungslokal.
      }
      if (!next) {
        stop();
      }
    },
    [stop],
  );

  const play = useCallback(
    async (text: string): Promise<void> => {
      const speakable = text.trim();
      if (!speakable) return;

      stop();
      const ownSequence = sequenceRef.current;
      const isCurrent = () => ownSequence === sequenceRef.current;

      try {
        const response = await api.speakText(speakable);
        if (!isCurrent() || !response.data_url) return;

        const audio = new Audio(response.data_url);
        audioRef.current = audio;

        await new Promise<void>((resolve, reject) => {
          let stall: number | null = null;

          const cleanupPlayback = () => {
            if (stall !== null) {
              window.clearTimeout(stall);
              stall = null;
            }
            audio.removeEventListener("ended", onEnded);
            audio.removeEventListener("error", onError);
            audio.removeEventListener("timeupdate", armStall);
            stopPlaybackRef.current = null;
          };

          const armStall = () => {
            if (stall !== null) {
              window.clearTimeout(stall);
            }
            stall = window.setTimeout(() => {
              cleanupPlayback();
              reject(new Error("Playback stalled"));
            }, PLAYBACK_STALL_MS);
          };

          const onEnded = () => {
            cleanupPlayback();
            resolve();
          };

          const onError = () => {
            cleanupPlayback();
            reject(new Error("Playback failed"));
          };

          stopPlaybackRef.current = () => {
            cleanupPlayback();
            resolve();
          };

          audio.addEventListener("ended", onEnded, { once: true });
          audio.addEventListener("error", onError, { once: true });
          audio.addEventListener("timeupdate", armStall);
          armStall();
          void audio.play().catch(onError);
        });

        if (isCurrent()) {
          audioRef.current = null;
        }
      } catch {
        // Best-effort: das Vorlesen blockiert NIE den Chat — Fehler nur als
        // State für optionales UI bereitstellen.
        if (isCurrent()) {
          audioRef.current = null;
          setSpeakError(t.speakError);
        }
      }
    },
    [stop],
  );

  return { play, stop, enabled, setEnabled, speakError };
}
