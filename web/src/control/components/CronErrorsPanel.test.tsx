import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CronErrorsPanel } from "./CronErrorsPanel";

describe("CronErrorsPanel", () => {
  it("renders errored cron job names and error text", () => {
    const html = renderToStaticMarkup(
      <CronErrorsPanel
        now={200}
        data={{
          errors: [
            {
              id: "job-1",
              name: "openclaw-morning-launchpad",
              lastError: "systemd unit failed",
              consecutiveErrors: 3,
              lastRunAt: 140,
            },
          ],
        }}
      />,
    );

    expect(html).toContain("openclaw-morning-launchpad");
    expect(html).toContain("systemd unit failed");
    expect(html).toContain("3 Fehler in Folge");
    expect(html).toContain("data-cron-error-row");
  });

  it("renders no error rows for an empty list", () => {
    const html = renderToStaticMarkup(<CronErrorsPanel data={{ errors: [] }} />);

    expect(html).toBe("");
    expect(html).not.toContain("data-cron-error-row");
  });
});
