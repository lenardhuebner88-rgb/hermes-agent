/**
 * watcherDigest — S5-Design: Beweis der Peripherie-Verdichtung.
 *  1. Parsing: Bundle-Zeilen → Events (State aus dem „— <state>"-Suffix,
 *     Beleg-Klammer und „- " abgeschnitten, Headerzeile übersprungen,
 *     Nicht-Watcher-Engines ignoriert).
 *  2. Dedupe: gleiche taskId (sonst gleicher Titel) → nur das NEUESTE Event
 *     überlebt (gave_up/review_wait_attention verschwinden hinter completed).
 *  3. Zähler laufen über die deduplizierte Menge (completedToday/attention-
 *     Open/blockedOpen) + lastEvent = neuestes dedupliziertes Event.
 */
import { describe, expect, it } from "vitest";

import type { PaChatMessage } from "@/lib/api";
import { digestWatcherEvents, parseWatcherEvents } from "./watcherDigest";

let msgId = 0;

function watcherMessage(content: string, ts: number): PaChatMessage {
  msgId += 1;
  return {
    id: msgId,
    turn_id: `turn_${msgId}`,
    role: "assistant",
    content,
    engine: "pa-watcher",
    model: "watcher",
    attachments: [],
    ts,
    status: "done",
    error: null,
  };
}

function chatMessage(content: string, ts: number): PaChatMessage {
  return { ...watcherMessage(content, ts), engine: "sol" };
}

const BUNDLE = [
  "Jarvis-Wächter: 2 signifikante Ereignisse gebündelt.",
  "- Jarvis Mobile: APK paketieren (t_492864de) — completed (Beleg: receipts/2026-07-20/t_492864de.md)",
  "- Vault-Sync (t_aa0011bb) — blocked:integration (Beleg: receipts/t_aa0011bb.md)",
].join("\n");

describe("parseWatcherEvents", () => {
  it("parst Bundle-Zeilen: State, Titel ohne - /Beleg/State-Suffix, taskId", () => {
    const events = parseWatcherEvents([watcherMessage(BUNDLE, 1700000000)]);

    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({
      taskId: "t_492864de",
      state: "completed",
      title: "Jarvis Mobile: APK paketieren",
      ts: 1700000000,
    });
    expect(events[1]).toEqual({
      taskId: "t_aa0011bb",
      state: "blocked",
      title: "Vault-Sync",
      ts: 1700000000,
    });
  });

  it("überspringt die Headerzeile und ignoriert Nicht-Watcher-Engines", () => {
    const events = parseWatcherEvents([
      chatMessage("- sieht aus wie ein Bundle — completed", 1700000000),
      watcherMessage(BUNDLE, 1700000001),
    ]);

    // Nur die zwei echten Watcher-Zeilen — die Chat-Zeile zählt nicht.
    expect(events).toHaveLength(2);
    expect(events.every((e) => e.ts === 1700000001)).toBe(true);
  });

  it("erkennt Receipt-/Session-Zeilen und unbekannte Zeilen als info", () => {
    const events = parseWatcherEvents([
      watcherMessage(
        [
          "Jarvis-Wächter: 3 signifikante Ereignisse gebündelt.",
          "- Neues Receipt: t_492864de abgeschlossen",
          "- Agenten-Session beendet: codex/jarvis-s4",
          "- irgendetwas anderes",
        ].join("\n"),
        1700000000,
      ),
    ]);

    expect(events.map((e) => e.state)).toEqual(["receipt", "session", "info"]);
    expect(events[0].taskId).toBe("t_492864de");
    expect(events[2].taskId).toBeNull();
  });

  it("mappt review_wait_attention auf attention und gave_up auf gave_up", () => {
    const events = parseWatcherEvents([
      watcherMessage(
        [
          "- Task A (t_11111111) — review_wait_attention (Beleg: r/1.md)",
          "- Task B (t_22222222) — gave_up (Beleg: r/2.md)",
        ].join("\n"),
        1700000000,
      ),
    ]);

    expect(events.map((e) => e.state)).toEqual(["attention", "gave_up"]);
  });
});

describe("digestWatcherEvents", () => {
  it("dedupliziert pro taskId: gave_up → review_wait_attention → completed = nur completed", () => {
    const digest = digestWatcherEvents([
      watcherMessage("- Jarvis Mobile (t_492864de) — gave_up (Beleg: r/a.md)", 1700000000),
      watcherMessage("- Jarvis Mobile (t_492864de) — review_wait_attention (Beleg: r/b.md)", 1700000100),
      watcherMessage("- Jarvis Mobile (t_492864de) — completed (Beleg: r/c.md)", 1700000200),
    ]);

    expect(digest.latest).toHaveLength(1);
    expect(digest.latest[0].state).toBe("completed");
    expect(digest.latest[0].title).toBe("Jarvis Mobile");
    expect(digest.attentionOpen).toBe(0);
    expect(digest.lastEvent?.state).toBe("completed");
  });

  it("dedupliziert ohne taskId über den Titel", () => {
    const digest = digestWatcherEvents([
      watcherMessage("- Nachtlauf — gave_up (Beleg: r/a.md)", 1700000000),
      watcherMessage("- Nachtlauf — completed (Beleg: r/b.md)", 1700000100),
    ]);

    expect(digest.latest).toHaveLength(1);
    expect(digest.latest[0].state).toBe("completed");
  });

  it("zählt attention/blocked nur, wenn kein späteres completed folgt", () => {
    const digest = digestWatcherEvents([
      watcherMessage(
        [
          "- Offen A (t_aaaaaaa1) — review_wait_attention (Beleg: r/1.md)",
          "- Offen B (t_bbbbbbb2) — blocked:integration (Beleg: r/2.md)",
        ].join("\n"),
        1700000000,
      ),
      watcherMessage("- Erledigt C (t_ccccccc3) — completed (Beleg: r/3.md)", 1700000100),
      watcherMessage("- War offen D (t_dddddd44) — review_wait_attention (Beleg: r/4.md)", 1700000200),
      watcherMessage("- War offen D (t_dddddd44) — completed (Beleg: r/5.md)", 1700000300),
    ]);

    expect(digest.attentionOpen).toBe(1); // nur A
    expect(digest.blockedOpen).toBe(1); // nur B
    expect(digest.lastEvent?.taskId).toBe("t_dddddd44");
    // Neueste zuerst in der latest-Liste.
    expect(digest.latest[0].taskId).toBe("t_dddddd44");
  });

  it("completedToday zählt nur Abschlüsse des laufenden Tages", () => {
    const now = Math.floor(Date.now() / 1000);
    const digest = digestWatcherEvents([
      watcherMessage("- Heute (t_11111111) — completed (Beleg: r/1.md)", now),
      watcherMessage("- Gestern (t_22222222) — completed (Beleg: r/2.md)", now - 2 * 24 * 3600),
    ]);

    expect(digest.completedToday).toBe(1);
  });

  it("begrenzt latest auf max und meldet bei leerer History den Leerzustand", () => {
    const messages = Array.from({ length: 7 }, (_, index) =>
      watcherMessage(`- Task ${index} (t_0000000${index}) — completed (Beleg: r.md)`, 1700000000 + index),
    );
    const digest = digestWatcherEvents(messages, { max: 3 });

    expect(digest.latest).toHaveLength(3);
    expect(digest.latest[0].title).toBe("Task 6"); // neueste zuerst

    const empty = digestWatcherEvents([]);
    expect(empty.latest).toEqual([]);
    expect(empty.lastEvent).toBeNull();
    expect(empty.completedToday).toBe(0);
  });
});
