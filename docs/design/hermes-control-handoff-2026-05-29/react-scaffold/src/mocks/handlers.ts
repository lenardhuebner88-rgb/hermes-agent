/**
 * MSW-Handler (msw v2) — bilden die echten Endpunkte mit den Fixtures ab.
 * Damit lässt sich das gesamte Frontend bauen & durchklicken, BEVOR das Backend
 * steht. apply/skip mutieren einen In-Memory-Stand, damit die UI realistisch
 * reagiert (Vorschlag wandert nach „Erledigt").
 *
 *   import { handlers } from '@/mocks/handlers'
 *   setupWorker(...handlers).start()   // siehe browser.ts
 */
import { http, HttpResponse } from 'msw';
import {
  hermesWorkers, openclawAgents, autoresearchStatus, proposals as seedProposals, NOW,
} from '../data/fixtures';
import type { Proposal } from '../lib/types';

// veränderbarer Stand (Demo)
let proposals: Proposal[] = seedProposals.map((p) => ({ ...p }));

export const handlers = [
  http.get('/api/plugins/kanban/workers/active', () =>
    HttpResponse.json({ workers: hermesWorkers, count: hermesWorkers.length, checked_at: NOW })),

  http.get('/runs/:runId/inspect', ({ params }) => {
    const w = hermesWorkers.find((x) => x.run_id === params.runId);
    if (!w) return new HttpResponse(null, { status: 404 });
    return HttpResponse.json({ ...w.inspect, run_id: w.run_id });
  }),

  http.get('/api/openclaw/agents', () =>
    HttpResponse.json({ agents: openclawAgents, updatedAt: NOW })),

  http.get('/autoresearch/status', () => HttpResponse.json(autoresearchStatus)),

  http.get('/autoresearch/proposals', () => HttpResponse.json(proposals)),

  http.post('/autoresearch/apply', async ({ request }) => {
    const { id } = (await request.json()) as { id: string };
    proposals = proposals.map((p) =>
      p.id === id
        ? { ...p, status: 'applied', applied_at: NOW,
            result: p.mode === 'code' ? '✓ übernommen — Code: Tests grün' : '✓ übernommen — Skill: eval grün' }
        : p);
    return HttpResponse.json({ ok: true, id });
  }),

  http.post('/autoresearch/skip', async ({ request }) => {
    const { id } = (await request.json()) as { id: string };
    proposals = proposals.map((p) => (p.id === id ? { ...p, status: 'skipped', result: 'übersprungen' } : p));
    return HttpResponse.json({ ok: true, id });
  }),
];

/** Reset für Tests / Storybook. */
export function resetMockProposals() {
  proposals = seedProposals.map((p) => ({ ...p }));
}
