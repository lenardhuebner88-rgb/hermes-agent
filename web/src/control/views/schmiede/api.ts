import { fetchJSON } from "@/lib/api";

export interface GenerateResponse {
  prompt: string;
  fallback: boolean;
  model?: string;
  error?: string;
}

export interface GenerateRequest {
  problem: string;
  targetId: string;
  taskTypeId: string;
  modeId: string;
  modelId: string;
}

/** Ask the backend to turn a plain-language problem into a polished prompt.
 *  The generator model is server-fixed (free Gemini Flash); modelId is only the
 *  target model the generated prompt should run on. Throws on network error —
 *  callers fall back to the deterministic local composer. */
export async function generatePrompt(req: GenerateRequest): Promise<GenerateResponse> {
  return fetchJSON<GenerateResponse>("/api/promptforge/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}
