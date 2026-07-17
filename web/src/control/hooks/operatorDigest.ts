import { fetchJSON } from "@/lib/api";
import { OperatorDigestResponseSchema, parseOrThrow } from "../lib/schemas";
import type { OperatorDigestResponse } from "../lib/types";
import { usePolling } from "./internal";

export function useOperatorDigest() {
  return usePolling<OperatorDigestResponse>(
    "operator/digest",
    async () => parseOrThrow(
      OperatorDigestResponseSchema,
      await fetchJSON<unknown>("/api/operator/digest"),
      "operator/digest",
    ),
    30000,
  );
}
