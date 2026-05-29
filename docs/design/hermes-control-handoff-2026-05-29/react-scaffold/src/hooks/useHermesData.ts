/**
 * SWR-Daten-Hooks. Eine Stelle für Fetch + Vertrags-Validierung (zod).
 * Live-Aktualisierung via `refreshInterval` (Polling) ODER per SSE → `mutate`
 * (siehe mocks/sse.ts). Apply/Skip sind optimistisch + revalidieren danach.
 */
import useSWR, { useSWRConfig } from 'swr';
import {
  WorkersResponseSchema, AgentsResponseSchema,
  AutoresearchStatusSchema, ProposalsResponseSchema, parseOrThrow,
} from '../lib/schemas';
import type {
  WorkersResponse, AgentsResponse, AutoresearchStatus, Proposal,
} from '../lib/types';

async function fetchJson(url: string): Promise<unknown> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`);
  return r.json();
}

export function useHermesWorkers() {
  return useSWR<WorkersResponse>(
    '/api/plugins/kanban/workers/active',
    async (u) => parseOrThrow(WorkersResponseSchema, await fetchJson(u), 'workers/active'),
    { refreshInterval: 5000 },
  );
}

export function useOpenClawAgents() {
  return useSWR<AgentsResponse>(
    '/api/openclaw/agents',
    async (u) => parseOrThrow(AgentsResponseSchema, await fetchJson(u), 'openclaw/agents'),
    { refreshInterval: 5000 },
  );
}

export function useAutoresearchStatus() {
  return useSWR<AutoresearchStatus>(
    '/autoresearch/status',
    async (u) => parseOrThrow(AutoresearchStatusSchema, await fetchJson(u), 'autoresearch/status'),
    { refreshInterval: 4000 },
  );
}

export function useProposals() {
  return useSWR<Proposal[]>(
    '/autoresearch/proposals',
    async (u) => parseOrThrow(ProposalsResponseSchema, await fetchJson(u), 'autoresearch/proposals'),
    { refreshInterval: 8000 },
  );
}

/** Apply/Skip mit optimistischem Update auf den Proposals-Cache. */
export function useProposalActions() {
  const { mutate } = useSWRConfig();
  const KEY = '/autoresearch/proposals';

  const patch = (id: string, next: Partial<Proposal>) =>
    mutate<Proposal[]>(
      KEY,
      async (current) => {
        const optimistic = (current ?? []).map((p) => (p.id === id ? { ...p, ...next } : p));
        return optimistic;
      },
      { revalidate: false, optimisticData: (c) => (c ?? []).map((p) => (p.id === id ? { ...p, ...next } : p)) },
    );

  const apply = async (id: string, mode: Proposal['mode']) => {
    await patch(id, { status: 'applied', result: mode === 'code' ? '✓ übernommen — Code: Tests grün' : '✓ übernommen — Skill: eval grün' });
    await fetch('/autoresearch/apply', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id }) });
    mutate(KEY); // revalidate
  };

  const skip = async (id: string) => {
    await patch(id, { status: 'skipped', result: 'übersprungen' });
    await fetch('/autoresearch/skip', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id }) });
    mutate(KEY);
  };

  const applyAll = async (proposals: Proposal[]) => {
    const open = proposals.filter((p) => p.status === 'proposed');
    await Promise.all(open.map((p) => apply(p.id, p.mode)));
  };

  return { apply, skip, applyAll };
}
