import { useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import type { PromptForgeCatalog } from "../views/schmiede/catalog";

export interface PromptForgeCatalogState {
  data: PromptForgeCatalog | null;
  error: string | null;
  loading: boolean;
  lastUpdated: number | null;
}

/** One-shot load of the static Prompt-Schmiede catalog. No polling — the catalog
 *  is static content served read-only from GET /api/promptforge/catalog. */

export function usePromptForgeCatalog(): PromptForgeCatalogState {
  const [data, setData] = useState<PromptForgeCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const payload = await fetchJSON<PromptForgeCatalog>("/api/promptforge/catalog");
        if (!aliveRef.current) return;
        setData(payload);
        setError(null);
        setLastUpdated(Math.floor(Date.now() / 1000));
      } catch (err) {
        if (!aliveRef.current) return;
        setError(err instanceof Error ? err.message : "Katalog konnte nicht geladen werden");
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    })();
  }, []);

  return { data, error, loading, lastUpdated };
}
