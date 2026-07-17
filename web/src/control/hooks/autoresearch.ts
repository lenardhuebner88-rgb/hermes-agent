import { fetchJSON } from "@/lib/api";
import { AutoresearchRunsResponseSchema, AutoresearchStatusSchema, parseOrThrow } from "../lib/schemas";
import type { AutoresearchRunsResponse, AutoresearchStatus } from "../lib/types";
import { usePolling } from "./internal";

export function useAutoresearchStatus() {
  return usePolling<AutoresearchStatus>(
    "autoresearch/status",
    async () => parseOrThrow(AutoresearchStatusSchema, await fetchJSON<unknown>("/api/autoresearch/status"), "autoresearch/status"),
    5000,
  );
}


export function useAutoresearchRuns() {
  return usePolling<AutoresearchRunsResponse>(
    "autoresearch/runs",
    async () => parseOrThrow(AutoresearchRunsResponseSchema, await fetchJSON<unknown>("/api/autoresearch/runs"), "autoresearch/runs"),
    10000,
  );
}
