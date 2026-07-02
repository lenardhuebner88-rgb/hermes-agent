import type { Proposal } from "./types";

// Live fixture provenance:
// Source intended by S5: GET /api/autoresearch/proposals after POST
// /auth/password-login with credentials from ~/.hermes/.env.
// Capture note: this Codex sandbox could not connect to 127.0.0.1:9119, so the
// equivalent local proposal-store payload was read via
// hermes_cli.autoresearch_proposals.proposals_payload() on 2026-07-02.
// Scope: the live Test-Foundry mutation-survivor subset (30 items), which is
// the grounded inbox-hygiene cluster this slice protects. No credentials,
// cookies, tokens, diff bodies, or secrets are stored here.

const LIVE_MUTATION_ROWS: Array<[string, string, string]> = [
  ["test-foundry-kanban-decompose-4ac5351d0326783c", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:653"],
  ["test-foundry-kanban-decompose-331e4e522cd9c7b9", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:319"],
  ["test-foundry-kanban-decompose-c3a437de9033cc06", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:303"],
  ["test-foundry-kanban-decompose-9475c95014000bc0", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:282"],
  ["test-foundry-kanban-decompose-46a134f56147631e", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:644"],
  ["test-foundry-kanban-decompose-1b386971659f7438", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:313"],
  ["test-foundry-kanban-decompose-a36783efc6c5db26", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:297"],
  ["test-foundry-kanban-decompose-86ee8da8c6922a4b", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:640"],
  ["test-foundry-kanban-decompose-f0a1d47f247b2362", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:309"],
  ["test-foundry-kanban-decompose-f5353aa57ee11ece", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:263"],
  ["test-foundry-kanban-decompose-cc6ea535f16aa40c", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:640"],
  ["test-foundry-kanban-decompose-6b8818be9f15cdf5", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:309"],
  ["test-foundry-kanban-decompose-47106ae55d41793c", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:293"],
  ["test-foundry-kanban-decompose-096e80340e2aa969", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:263"],
  ["test-foundry-kanban-decompose-835015e64d0afac2", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:248"],
  ["test-foundry-kanban-decompose-408a27b38a2f0ec1", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:567"],
  ["test-foundry-kanban-decompose-e9d10b62d69096f6", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:306"],
  ["test-foundry-kanban-decompose-4230e5144adec1e9", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:286"],
  ["test-foundry-kanban-decompose-13def0ed5f567a6c", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:270"],
  ["test-foundry-kanban-decompose-8bf80746a59984fe", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:240"],
  ["test-foundry-kanban-decompose-33b794d777f8dd3f", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:516"],
  ["test-foundry-kanban-decompose-1806b217f0c70860", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:271"],
  ["test-foundry-kanban-decompose-a5bc185bc8b1e2c9", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:268"],
  ["test-foundry-kanban-decompose-840c3d22b42245f5", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:248"],
  ["test-foundry-kanban-decompose-d2f07d71fae746e5", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:202"],
  ["test-foundry-kanban-decompose-45b3e7444bb902f7", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:187"],
  ["test-foundry-kanban-db-519481782009bb2b", "hermes_cli/kanban_db.py", "Mutation survivor in hermes_cli/kanban_db.py:421"],
  ["test-foundry-kanban-db-7855dfe91cc1eb02", "hermes_cli/kanban_db.py", "Mutation survivor in hermes_cli/kanban_db.py:372"],
  ["test-foundry-kanban-decompose-8bbe3479430c12fa", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:243"],
  ["test-foundry-kanban-decompose-0b5e23abf7080736", "hermes_cli/kanban_decompose.py", "Mutation survivor in hermes_cli/kanban_decompose.py:227"],
];

export const liveAutoresearchMutationFixture: Proposal[] = LIVE_MUTATION_ROWS.map(([id, target, title], index) => ({
  id,
  target,
  section: null,
  title,
  category: "mutation_survivor",
  severity: "high",
  rationale_plain: "Live Test-Foundry mutation survivor awaiting operator review.",
  diff_before_after: "",
  rank_score: 100 - index,
  mode: "test",
  status: "proposed",
  last_outcome: null,
  result: null,
  created_at: "2026-07-02T00:00:00Z",
  applied_at: null,
  gate: null,
}));
