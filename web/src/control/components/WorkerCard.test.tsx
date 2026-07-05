import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { WorkerCard } from "./WorkerCard";
import { STUCK_HEARTBEAT_S, workerHealth } from "../lib/derive";
import type { Worker } from "../lib/types";

const NOW = 1_780_041_720;

function mkWorker(over: Partial<Worker> = {}): Worker {
  return {
    run_id: "run_worker_panel",
    task_id: "t_worker_panel",
    task_title: "Worker-Panel Regression",
    task_status: "running",
    task_assignee: "coder",
    profile: "coder",
    worker_pid: 4242,
    started_at: NOW - 300,
    claim_lock: "lock",
    claim_expires: NOW + 600,
    last_heartbeat_at: NOW - 10,
    max_runtime_seconds: 3_600,
    run_status: "running",
    run_outcome: null,
    inspect: { cpu_percent: 10, rss: 128 * 1_048_576, num_threads: 2, num_fds: 7, status: "running", alive: true },
    ...over,
  };
}

function renderWorker(worker: Worker) {
  return renderToStaticMarkup(
    <WorkerCard
      worker={worker}
      health={workerHealth(worker, NOW)}
      density="compact"
      now={NOW}
      onInspect={() => undefined}
      onAction={() => undefined}
    />,
  );
}

describe("WorkerCard error and status copy", () => {
  it("renders the stuck worker state with the final German status and reason", () => {
    const html = renderWorker(mkWorker({
      task_status: "ready",
      last_heartbeat_at: NOW - (STUCK_HEARTBEAT_S + 1),
    }));

    expect(html).toContain("Startklar");
    expect(html).toContain("Hängt");
    expect(html).toContain("Heartbeat 10m alt oder Claim abgelaufen");
    expect(html).not.toContain("Stuck");
  });

  it("renders blocked review workers with the German review label and block text", () => {
    const html = renderWorker(mkWorker({
      task_status: "review",
      run_status: "blocked",
      block_reason: "Operator-Entscheidung fehlt",
    }));

    expect(html).toContain("In Prüfung");
    expect(html).toContain("Blockiert");
    expect(html).toContain("Operator-Entscheidung fehlt");
    expect(html).not.toContain("Review");
  });

  it("renders offline workers with the unified German process error", () => {
    const html = renderWorker(mkWorker({
      run_status: "crashed",
      inspect: { cpu_percent: 0, rss: 0, num_threads: 0, num_fds: 0, status: "dead", alive: false },
    }));

    expect(html).toContain("Offline");
    expect(html).toContain("Prozess reagiert nicht mehr");
  });

  it("renders runaway risk and critical badges without English runaway copy", () => {
    const warningHtml = renderWorker(mkWorker({ started_at: NOW - 3_000, max_runtime_seconds: 3_600 }));
    const criticalHtml = renderWorker(mkWorker({ started_at: NOW - 3_700, max_runtime_seconds: 3_600 }));

    expect(warningHtml).toContain("Entgleisungsrisiko");
    expect(criticalHtml).toContain("Entgleist");
    expect(`${warningHtml} ${criticalHtml}`).not.toMatch(/Runaway/i);
  });

  it("keeps the card root shrinkable (min-w-0) so it never overflows a grid/flex column", () => {
    // Regression: the card is a grid item (FleetPanel `grid lg:grid-cols-2`, the
    // chain-node cockpit). A grid/flex item defaults to min-width:auto and refuses to
    // shrink below its content → on a narrow single-column (mobile) the card overflowed
    // the viewport to the right (clipped by the page's overflow-x-hidden → content cut
    // off). Layout can't be measured in jsdom, so guard the load-bearing class itself.
    const html = renderWorker(mkWorker());
    expect(html).toMatch(/<article[^>]*\bmin-w-0\b/);
  });
});


describe("WorkerCard time axis accessibility", () => {
  it("exposes the time axis as an img with a label summarizing now, p50, p90 and budget", () => {
    const html = renderWorker(mkWorker({
      started_at: NOW - 300,
      eta_p50_seconds: 480,
      eta_p90_seconds: 900,
      max_runtime_seconds: 1_800,
    }));

    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="Zeit-Achse: jetzt 5m, p50 8m, p90 15m, Budget 30m"');
  });

  it("omits missing p50/p90 values from the accessible label", () => {
    const html = renderWorker(mkWorker({
      started_at: NOW - 300,
      eta_p50_seconds: null,
      eta_p90_seconds: null,
      max_runtime_seconds: 1_800,
    }));

    expect(html).toContain('aria-label="Zeit-Achse: jetzt 5m, Budget 30m"');
    expect(html).not.toContain("p50 0");
    expect(html).not.toContain("p90 0");
  });
});
