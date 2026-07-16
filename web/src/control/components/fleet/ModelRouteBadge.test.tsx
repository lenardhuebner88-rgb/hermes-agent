// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { WorkerSchema } from "../../lib/schemas";
import { ModelRouteBadge } from "./ModelRouteBadge";

afterEach(cleanup);

describe("ModelRouteBadge", () => {
  it.each([
    ["planned", "geplant"],
    ["in_flight", "angefragt"],
    ["confirmed", "bestätigt"],
  ] as const)("renders %s with full provider and model", (modelState, label) => {
    render(
      <ModelRouteBadge
        requestedProvider="openai-codex"
        requestedModel="gpt-5.6-sol"
        activeProvider="openai-codex"
        activeModel="gpt-5.6-sol-20260713-long-route"
        modelState={modelState}
        modelSource="provider_response"
        observedAt={1_783_960_000}
      />,
    );

    expect(screen.getByText("openai-codex · gpt-5.6-sol-20260713-long-route")).toBeTruthy();
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.getByLabelText(/Quelle: provider_response/)).toBeTruthy();
  });

  it("renders legacy inference explicitly and never calls it confirmed", () => {
    render(
      <ModelRouteBadge
        activeProvider="openrouter"
        activeModel="legacy/full-model-name"
        modelState="unknown"
        modelSource="legacy_inferred"
      />,
    );

    expect(screen.getByText("openrouter · legacy/full-model-name")).toBeTruthy();
    expect(screen.getByText("abgeleitet")).toBeTruthy();
    expect(screen.queryByText("bestätigt")).toBeNull();
  });

  it("shows an explicit warning when model telemetry is absent", () => {
    render(<ModelRouteBadge modelState="unknown" modelSource={null} hasRun />);

    expect(screen.getByText("Modell unbekannt – Telemetrie fehlt").className).toContain("text-status-warn");
  });

  it("renders an unstarted step neutrally when no run exists", () => {
    render(<ModelRouteBadge modelState="unknown" modelSource={null} hasRun={false} />);

    const label = screen.getByText("Noch kein Run");
    expect(label.className).not.toContain("text-status-warn");
    expect(screen.queryByText("Modell unbekannt – Telemetrie fehlt")).toBeNull();
  });

  it("updates the visible route when a fallback prop arrives", () => {
    const { rerender } = render(
      <ModelRouteBadge
        activeProvider="openai-codex"
        activeModel="gpt-5.6-sol"
        modelState="in_flight"
        modelSource="runtime_request"
      />,
    );
    expect(screen.getByText("openai-codex · gpt-5.6-sol")).toBeTruthy();

    rerender(
      <ModelRouteBadge
        activeProvider="kimi-coding"
        activeModel="kimi-k2.7-code"
        modelState="confirmed"
        modelSource="provider_response"
      />,
    );

    expect(screen.getByText("kimi-coding · kimi-k2.7-code")).toBeTruthy();
    expect(screen.queryByText("openai-codex · gpt-5.6-sol")).toBeNull();
  });
});

describe("WorkerSchema model-route compatibility", () => {
  it("accepts additive route fields without dropping a valid worker", () => {
    const parsed = WorkerSchema.parse({
      run_id: 42,
      requested_provider: "openai-codex",
      requested_model: "gpt-5.6-sol",
      active_provider: "openai-codex",
      active_model: "gpt-5.6-sol-20260713",
      model_state: "confirmed",
      model_source: "provider_response",
      model_observed_at: 1_783_960_000,
      effective_model: "gpt-5.6-sol-20260713",
    });

    expect(parsed.run_id).toBe("42");
    expect(parsed.active_provider).toBe("openai-codex");
    expect(parsed.active_model).toBe("gpt-5.6-sol-20260713");
    expect(parsed.model_state).toBe("confirmed");
    expect(parsed.effective_model).toBe("gpt-5.6-sol-20260713");
  });
});
