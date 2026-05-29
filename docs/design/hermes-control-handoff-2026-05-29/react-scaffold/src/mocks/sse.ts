/**
 * SSE-Simulator für lokale Entwicklung ohne Backend.
 * Die echte App abonniert einen Server-Sent-Events-Strom (z.B. /autoresearch/stream
 * und /api/.../stream) und füttert SWR per `mutate`. Bis der Strom existiert,
 * liefert `createMockEventStream` periodische Frames mit leicht wandernden Werten,
 * damit Heartbeats/„läuft"-Zustände lebendig wirken.
 *
 *   const stream = createMockEventStream((evt) => {
 *     if (evt.type === 'autoresearch:status') mutate('/autoresearch/status', evt.data, false);
 *   });
 *   // später: stream.close();
 */
import { autoresearchStatus } from '../data/fixtures';
import type { AutoresearchStatus } from '../lib/types';

export type StreamEvent =
  | { type: 'autoresearch:status'; data: AutoresearchStatus }
  | { type: 'heartbeat'; data: { at: number } };

export interface MockStream { close: () => void; }

export function createMockEventStream(
  onEvent: (e: StreamEvent) => void,
  intervalMs = 4000,
): MockStream {
  let age = autoresearchStatus.heartbeat_age_s;
  const id = setInterval(() => {
    age = age >= 8 ? 1 : age + 1; // Heartbeat „atmet"
    onEvent({
      type: 'autoresearch:status',
      data: { ...autoresearchStatus, heartbeat_age_s: age, heartbeat_fresh: age < 15 },
    });
    onEvent({ type: 'heartbeat', data: { at: Math.floor(Date.now() / 1000) } });
  }, intervalMs);
  return { close: () => clearInterval(id) };
}

/**
 * Produktions-Skizze (zur Orientierung, nicht aktiv):
 *
 *   export function connectStatusStream(onStatus: (s: AutoresearchStatus) => void) {
 *     const es = new EventSource('/autoresearch/stream');
 *     es.addEventListener('status', (e) =>
 *       onStatus(AutoresearchStatusSchema.parse(JSON.parse(e.data))));
 *     return () => es.close();
 *   }
 */
