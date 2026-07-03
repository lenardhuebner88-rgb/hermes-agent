import { describe, expect, it } from "vitest";
import { parseLedgerLine } from "./loopLedger";

// Fixture-Herkunft (Piet-Direktive: echte Daten statt handgeschriebener Fakes).
//
// Verifiziert am 2026-07-03: KEIN Pack unter /home/piet/.hermes/loops/*/ (dem
// State-Root des laufenden /control-Loop-Runners) hat bislang eine LEDGER.md —
// der Runner wurde erst heute deployed, `builder-reviewer` hat noch keine
// abgeschlossene Runde, `test-stabiliser` läuft gerade seine erste Runde
// (`heartbeat.json`, `queue/*` leer). Die Prämisse "LEDGER.md existiert" aus
// dem Auftrag ist damit für dieses Repo (noch) falsch — belegt per `ls` +
// `journalctl --user -u 'hermes-loop@*'`.
//
// Ersatz-Quellen für echte Zeilen:
//  (a) REAL — zwei tatsächlich vorhandene Journal-Dateien auf diesem Host,
//      wörtlich zitiert: `/home/piet/.hermes/fable-loop/LEDGER.md` (eigener
//      Nachtloop-Harness, gleiche "- Datum Zeit …"-Konvention) und
//      `/home/piet/.hermes/kimi-loop/LEDGER.md` (andere Markdown-Tabellen-
//      Konvention). Beide sind KEIN Output von `loops/runner.py` — deshalb
//      als Grenzfälle für den defensiven Fallback verwendet, nicht als
//      Beleg für die geparsten Felder.
//  (b) AUTORITATIV — für Zeilenformen, die real (noch) nicht vorkommen
//      (build-fail, verify-fail, usage-limit, bounced, LAND, sweep-Status),
//      wörtlich aus den f-Strings in `loops/runner.py::LoopRunner.ledger()`-
//      Aufrufen rekonstruiert (derselbe Code, der `/api/loops/<pack>` speist)
//      und mit den echten Pack-Namen dieses Repos (`loops/packs/*`) befüllt.

describe("parseLedgerLine — REAL: /home/piet/.hermes/fable-loop/LEDGER.md", () => {
  it("parses a PLAN line with a filled status", () => {
    expect(parseLedgerLine("- 2026-07-03 03:10 PLAN: 5 Pläne (status=PLANNED 5)")).toEqual({
      verdict: null,
      phase: "plan",
      raw: "- 2026-07-03 03:10 PLAN: 5 Pläne (status=PLANNED 5)",
    });
  });

  it("parses a PLAN line with an empty status (defensive: kein Crash bei leerem Wert)", () => {
    const line = "- 2026-07-02 20:12 PLAN: 0 Pläne (status=)";
    expect(parseLedgerLine(line)).toEqual({ verdict: null, phase: "plan", raw: line });
  });

  it("parses a verified round line without a duration bracket (older/other generator omits it)", () => {
    const line = "- 2026-07-03 03:29 R1 ✅ P1-fl-heiler-sweep-signal-fidelity.md verified (38d279efc)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "ok", round: 1, phase: "verify", raw: line });
  });

  it("parses a later round with a two-digit-safe round number", () => {
    const line = "- 2026-07-03 04:16 R5 ✅ P3-fl-held-complete-verb.md verified (84af4bad8)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "ok", round: 5, phase: "verify", raw: line });
  });
});

describe("parseLedgerLine — REAL: fremde Ledger-Formate bleiben unangetastet (Fallback, kein Crash)", () => {
  it("falls back on a kimi-loop Markdown-table row (/home/piet/.hermes/kimi-loop/LEDGER.md)", () => {
    const line =
      "| 2026-06-27 23:53 | FIXED | bibliothek | hermes_cli/library_state.py:291 | set_topic_follow persistierte Demo-Topics auch bei followed=False, obwohl sie virtuell bleiben sollten | 795d26b00 |";
    expect(parseLedgerLine(line)).toEqual({ verdict: null, raw: line });
  });

  it("falls back on a fable-loop OPERATOR-tagged line (custom tag outside the runner.py vocabulary)", () => {
    const line =
      "- 2026-07-02 21:52 OPERATOR: 20:07-Lauf traf Session-Limit (Reset 21:50) → 0 Pläne; Neustart per systemd-Timer 02:00 geplant; Limit-Regex in start.sh erweitert";
    expect(parseLedgerLine(line)).toEqual({ verdict: null, raw: line });
  });

  it("falls back on a fable-loop stamp without HH:MM (date-only PLANNER line)", () => {
    const line =
      "- 2026-07-03 PLANNER: 5 Pläne — fl-20260703-heiler-sweep-signal-fidelity (P1), fl-20260703-lane-catalog-pinned-models (P1)";
    expect(parseLedgerLine(line)).toEqual({ verdict: null, raw: line });
  });

  it("falls back on a Markdown header line", () => {
    const line = "# LEDGER — Fable-Nachtloop (append-only Journal)";
    expect(parseLedgerLine(line)).toEqual({ verdict: null, raw: line });
  });

  it("falls back on an empty string", () => {
    expect(parseLedgerLine("")).toEqual({ verdict: null, raw: "" });
  });
});

describe("parseLedgerLine — AUTORITATIV: loops/runner.py-Formate (echte Pack-Namen dieses Repos)", () => {
  it("parses a verified round with a build+verify duration bracket, summing both", () => {
    const line =
      "- 2026-07-03 07:14 R1 ✅ P1-repo-housekeeper-dead-code-sweep.md verified (a1b2c3d4e) [build 812s · verify 340s]";
    expect(parseLedgerLine(line)).toEqual({ verdict: "ok", round: 1, phase: "verify", secs: 1152, raw: line });
  });

  it("parses a build-fail line", () => {
    const line = "- 2026-07-03 07:26 R2 ❌ P2-doc-sweep-broken-links.md build-fail: BUILD_FAIL lint";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", round: 2, phase: "build", raw: line });
  });

  it("parses a verify-fail (reverted) line", () => {
    const line = "- 2026-07-03 07:41 R3 ❌ P1-error-sweep-flaky-retry.md verify-fail: FAIL pytest (reverted)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", round: 3, phase: "verify", raw: line });
  });

  it("parses an UNVERIFIED-but-committed usage-limit line as warn/build", () => {
    const line =
      "- 2026-07-03 07:55 R4 ⚠️ P2-loop-tuner-timeout-budget.md Commit vorhanden aber UNVERIFIED (usage-limit im Build)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "warn", round: 4, phase: "build", raw: line });
  });

  it("parses a no-commit usage-limit requeue line as pause/build", () => {
    const line =
      "- 2026-07-03 08:02 R5 ⏸ P3-promise-to-proof-claim-check.md zurück in die Queue (usage-limit, kein Commit)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "pause", round: 5, phase: "build", raw: line });
  });

  it("parses a bounced line, inferring the phase from the 'build:' reason prefix", () => {
    const line = "- 2026-07-03 05:40 bounced: P2-kimi-audit-dead-import.md (build: BUILD_FAIL lint gate)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", phase: "build", raw: line });
  });

  it("parses a bounced line, inferring the phase from the 'verify:' reason prefix", () => {
    const line = "- 2026-07-03 05:52 bounced: P3-builder-reviewer-race-fix.md (verify: FAIL gate-timeout)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", phase: "verify", raw: line });
  });

  it("parses a sweep DRY round as pause (nothing new found)", () => {
    const line = "- 2026-07-03 06:12 R3 sweep status=DRY [round 95s]";
    expect(parseLedgerLine(line)).toEqual({ verdict: "pause", round: 3, phase: "round", secs: 95, raw: line });
  });

  it("parses a sweep BLOCKED round as fail", () => {
    const line = "- 2026-07-03 06:34 R4 sweep status=BLOCKED [round 210s]";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", round: 4, phase: "round", secs: 210, raw: line });
  });

  it("parses a productive sweep round (any non-DRY/BLOCKED status) as ok", () => {
    const line = "- 2026-07-03 06:50 R5 sweep status=FIXED [round 178s]";
    expect(parseLedgerLine(line)).toEqual({ verdict: "ok", round: 5, phase: "round", secs: 178, raw: line });
  });

  it("parses a successful LAND line", () => {
    const line =
      "- 2026-07-03 07:15 LAND ✅ 3 Commits → main d4e5f6a7b (Anker loop-land/builder-reviewer/20260703-071500, 3 Pläne archiviert) · piet-fork gepusht";
    expect(parseLedgerLine(line)).toEqual({ verdict: "land", raw: line });
  });

  it("parses an aborted (non-ff) LAND line as fail", () => {
    const line = "- 2026-07-03 04:02 LAND abgebrochen: nicht ff-fähig (base 9a8b7c6d5)";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", raw: line });
  });

  it("parses a rolled-back LAND line as fail", () => {
    const line = "- 2026-07-03 03:50 LAND rollback (Anker loop-land/test-stabiliser/20260703-035000): pytest FAILED 2 tests";
    expect(parseLedgerLine(line)).toEqual({ verdict: "fail", raw: line });
  });

  it("parses a failed-revert line as warn", () => {
    const line = "- 2026-07-03 02:14 ⚠️ REVERT FEHLGESCHLAGEN (9a8b7c6..HEAD): error: pathspec did not match";
    expect(parseLedgerLine(line)).toEqual({ verdict: "warn", raw: line });
  });
});
