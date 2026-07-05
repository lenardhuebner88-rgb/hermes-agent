import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TopDecision } from "./CommandHome";
import type { InboxItem } from "../lib/decisionInbox";
import type {
  useFixRedispatch,
  useRepairDeliverable,
  useVetoEscalation,
} from "../hooks/useControlData";

const noopMutation = {
  busyId: null,
  doneIds: {},
  errorById: {},
  run: async () => undefined,
};

describe("TopDecision", () => {
  it("keeps long decision title and reason available while clamping the visible card", () => {
    const item: InboxItem = {
      key: "decision-1",
      surface: "autoresearch",
      title: "Skill-Schwäche in family-organizer-ui-polish: widersprüchliche Anweisung mit sehr langem Kontext",
      why: "contradiction · critical · mehrere Belege im Autoresearch-Report",
      nextAction: "Prüfen & entscheiden",
      tone: "red",
      target: "/control/autoresearch",
      weight: 95,
    };
    const html = renderToStaticMarkup(
      <TopDecision
        item={item}
        onOpen={() => undefined}
        fix={noopMutation as unknown as ReturnType<typeof useFixRedispatch>}
        repair={noopMutation as unknown as ReturnType<typeof useRepairDeliverable>}
        veto={noopMutation as unknown as ReturnType<typeof useVetoEscalation>}
      />,
    );

    expect(html).toContain(`title="${item.title}"`);
    expect(html).toContain(`title="${item.why}"`);
    expect(html).toContain("line-clamp-3");
    expect(html).toContain("sm:line-clamp-2");
  });
});
