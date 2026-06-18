import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useBoard, useChainGraph } from "../hooks/useControlData";
import { buildChains } from "../lib/fleet";
import { fmtAge, nowSec } from "../lib/derive";
import { Hero } from "../components/Hero";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { de } from "../i18n/de";
import { ChainSelector } from "./ketten/ChainSelector";
import { KettenGraph } from "./ketten/KettenGraph";

function ChainPanel({ rootId }: { rootId: string }) {
  const graph = useChainGraph(rootId);
  const now = nowSec();

  if (graph.error) {
    return (
      <div className="hc-surface-card p-4">
        <p className="text-sm text-red-700">{de.ketten.loadError}</p>
        <p className="mt-1 text-xs text-[var(--hc-text-dim)]">{graph.error}</p>
      </div>
    );
  }
  if (graph.loading && !graph.data) {
    return <SkeletonCard rows={3} />;
  }
  if (!graph.data) return null;

  return (
    <>
      <KettenGraph nodes={graph.data.nodes} edges={graph.data.edges} rootId={graph.data.root_id} />
      <p className="text-right text-xs text-[var(--hc-text-dim)]">
        {de.ketten.checkedAt(fmtAge(graph.data.checked_at, now))}
      </p>
    </>
  );
}

export function ChainVizView(_props: { density?: unknown }) {
  const [params, setParams] = useSearchParams();
  const board = useBoard();
  const [selectedRootId, setSelectedRootId] = useState<string | null>(null);

  const activeChains = useMemo(() => {
    if (!board.data) return [];
    const allTasks = board.data.columns.flatMap((c) => c.tasks);
    return buildChains(allTasks).active;
  }, [board.data]);

  // URL-param ?root= wins; fall back to user state, then first active chain.
  const requestedRoot = params.get("root")?.trim() || null;
  const focusedRootId = useMemo(() => {
    if (activeChains.length === 0) return null;
    // URL param takes precedence if it exists in active chains.
    if (requestedRoot && activeChains.some((c) => c.rootId === requestedRoot)) {
      return requestedRoot;
    }
    // User selection via ChainSelector (local state).
    if (selectedRootId && activeChains.some((c) => c.rootId === selectedRootId)) {
      return selectedRootId;
    }
    // Default: first active chain.
    return activeChains[0].rootId;
  }, [activeChains, requestedRoot, selectedRootId]);

  // Keep URL in sync when user selects via selector.
  function handleSelect(rootId: string) {
    setSelectedRootId(rootId);
    setParams(rootId ? { root: rootId } : {}, { replace: true });
  }

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <p className="hc-eyebrow">{de.ketten.eyebrow}</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-normal text-[var(--hc-text)]">
            {de.ketten.title}
          </h1>
          <p className="mt-1 text-sm text-[var(--hc-text-soft)]">{de.ketten.subtitle}</p>
        </div>
      </header>

      {board.loading && !board.data ? (
        <SkeletonCard rows={4} />
      ) : board.error && !board.data ? (
        <Hero eyebrow={de.ketten.eyebrow} tone="red" title={de.ketten.loadError} subtitle={board.error} />
      ) : activeChains.length === 0 ? (
        <Hero
          eyebrow={de.ketten.eyebrow}
          tone="zinc"
          title={de.ketten.emptyTitle}
          subtitle={de.ketten.emptyDesc}
        />
      ) : (
        <div className="grid gap-4">
          <div className="hc-surface-card p-3 lg:max-w-md">
            <Eyebrow>{de.ketten.chooseChain}</Eyebrow>
            <div className="mt-2">
              <ChainSelector
                chains={activeChains}
                selectedRootId={focusedRootId}
                onSelect={handleSelect}
                disabled={board.loading}
              />
            </div>
          </div>

          {focusedRootId ? <ChainPanel key={focusedRootId} rootId={focusedRootId} /> : null}
        </div>
      )}
    </div>
  );
}
