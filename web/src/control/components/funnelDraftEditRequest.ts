export function funnelDraftEditRequest(draftId: string, text: string, note: string): [string, RequestInit] {
  return [
    `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(draftId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_text: text, operator_note: note }),
    },
  ];
}
