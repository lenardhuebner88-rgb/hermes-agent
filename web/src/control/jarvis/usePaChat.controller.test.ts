// @vitest-environment jsdom
/**
 * G2b — usePaChat.resetState (Hook-Ebene, NEUE Datei):
 *  - Alt-Seiten + Cursor leeren, danach frischer Reload
 *  - Generation-Bump invalidiert in-flight Turn (keine späte activeTurn-Bubble)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import type { PaChatMessage, PaTurn } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice } from "./engineSelection";

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      listPaMessages: listPaMessagesMock,
      sendPaMessage: sendPaMessageMock,
      getPaTurn: getPaTurnMock,
    },
  };
});

import { usePaChat } from "./usePaChat";

let serverMessages: PaChatMessage[] = [];
let serverNextBeforeId: number | null = null;
let msgId = 0;

function makeMessage(overrides: Partial<PaChatMessage>): PaChatMessage {
  msgId += 1;
  return {
    id: msgId,
    turn_id: `turn_${msgId}`,
    role: "user",
    content: "",
    engine: "sol",
    model: "gpt-5.6-sol",
    attachments: [],
    ts: 1700000000,
    status: "done",
    error: null,
    ...overrides,
  };
}

function turnResponse(overrides: Partial<PaTurn> = {}): PaTurn {
  return {
    turn_id: "turn_hook",
    status: "done",
    reply: null,
    engine: "sol",
    model: "gpt-5.6-sol",
    ts: 1700000000,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  _resetPollingStore();
  _resetEngineChoice();
  serverMessages = [];
  serverNextBeforeId = null;
  msgId = 0;
  listPaMessagesMock.mockImplementation(async (_limit?: number, beforeId?: number) => {
    if (beforeId != null) {
      return {
        messages: [
          makeMessage({ id: 11, role: "user", content: "alt" }),
          makeMessage({ id: 12, role: "assistant", content: "alt-antwort" }),
        ],
        next_before_id: null,
      };
    }
    return { messages: serverMessages, next_before_id: serverNextBeforeId };
  });
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_hook" });
  getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply: "ok" }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  _resetEngineChoice();
});

describe("usePaChat G2b resetState", () => {
  it("leert olderMessages/Cursor und lädt den frischen Server-Stand", async () => {
    serverMessages = [
      makeMessage({ id: 21, role: "user", content: "jung" }),
      makeMessage({ id: 22, role: "assistant", content: "jung-antwort" }),
    ];
    serverNextBeforeId = 21;

    const { result } = renderHook(() => usePaChat({ turnPollIntervalMs: 20 }));

    await waitFor(() => {
      expect(result.current.messages?.some((m) => m.content === "jung-antwort")).toBe(true);
    });
    expect(result.current.nextBeforeId).toBe(21);

    await act(async () => {
      await result.current.loadOlder();
    });
    await waitFor(() => {
      expect(result.current.messages?.some((m) => m.content === "alt-antwort")).toBe(true);
    });
    expect(result.current.nextBeforeId).toBeNull();

    serverMessages = [makeMessage({ id: 31, role: "assistant", content: "frisch" })];
    serverNextBeforeId = 31;

    act(() => {
      result.current.resetState();
    });

    await waitFor(() => {
      expect(result.current.messages?.some((m) => m.content === "frisch")).toBe(true);
      expect(result.current.messages?.some((m) => m.content === "alt-antwort")).toBeFalsy();
    });
    expect(result.current.nextBeforeId).toBe(31);
    expect(result.current.activeTurn).toBeNull();
  });

  it("invalidiert in-flight Turn: späte Antwort setzt activeTurn nicht neu", async () => {
    let resolveTurn: ((value: PaTurn) => void) | null = null;
    getPaTurnMock.mockImplementation(
      () =>
        new Promise<PaTurn>((resolve) => {
          resolveTurn = resolve;
        }),
    );

    const { result } = renderHook(() => usePaChat({ turnPollIntervalMs: 20 }));
    await waitFor(() => expect(result.current.messagesLoading).toBe(false));

    await act(async () => {
      void result.current.send("hängender turn");
    });
    await waitFor(() => {
      expect(result.current.activeTurn?.phase).toBe("waiting");
      expect(result.current.activeTurn?.text).toBe("hängender turn");
    });
    // Poll-Schleife muss getPaTurn einmal angefasst haben (sonst war noch
    // kein „später“ Response-Promise offen).
    await waitFor(() => expect(getPaTurnMock).toHaveBeenCalled());
    expect(resolveTurn).toBeTypeOf("function");

    act(() => {
      result.current.resetState();
    });
    await waitFor(() => {
      expect(result.current.activeTurn).toBeNull();
    });

    await act(async () => {
      resolveTurn!(
        turnResponse({
          turn_id: "turn_hook",
          status: "done",
          reply: "zu spät",
        }),
      );
      await new Promise((r) => setTimeout(r, 80));
    });

    expect(result.current.activeTurn).toBeNull();
    expect(result.current.messages?.some((m) => m.content === "zu spät")).toBeFalsy();
  });
});
