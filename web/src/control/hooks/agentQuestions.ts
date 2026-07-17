import { api, type AgentQuestionEvent } from "@/lib/api";
import { usePolling } from "./internal";

/**
 * Open agent-questions (Frage-Assistent store) — polled for the Antwort-Sheet.
 * Answer POSTs stay in the sheet handler; this hook only loads/reloads the list.
 */
export function useAgentQuestions() {
  return usePolling<{ questions: AgentQuestionEvent[] }>(
    "agent-questions/open",
    () => api.listAgentQuestions(),
    5000,
  );
}
