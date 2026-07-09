---
title: "Prompting-Guide: Claude Opus"
type: model
tags:
  - llm-wiki
  - prompting
family: claude-opus
model_ids:
  - anthropic/claude-opus-4
  - anthropic/claude-opus-4.1
  - anthropic/claude-opus-4.5
  - anthropic/claude-opus-4.6
  - anthropic/claude-opus-4.7
  - anthropic/claude-opus-4.7-fast
  - anthropic/claude-opus-4.8
  - anthropic/claude-opus-4.8-fast
updated: 2026-07-09
maturity: curated
sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
  - https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
---

# Prompting-Guide: Claude Opus

Kuratiert aus Anthropics offizieller Doku (Stand 2026-07-09). Modellspezifisches Detail stammt
aus der Opus-4.8-Seite; ältere Opus-Versionen (4/4.1/4.5–4.7) teilen laut Doku dieselbe
allgemeine Prompting-Best-Practices-Seite, ohne eigene Versions-Unterseite — Migrationshinweise
dort gelten primär für den Sprung von 4.7 auf 4.8.

## Stärken & Schwächen

Laut Doku hat Claude Opus 4.8 "particular strengths in long-horizon agentic work, knowledge
work, vision, and memory tasks" und funktioniert "well out of the box" mit bestehenden
Opus-4.7-Prompts. Charakteristisch: Antwortlänge wird an die wahrgenommene Aufgaben-Komplexität
kalibriert statt an eine fixe Verbosity — kurze Antworten bei einfachen Lookups, deutlich
längere bei offener Analyse. Das Modell interpretiert Prompts **literaler** als Vorgänger,
besonders bei niedrigeren Effort-Stufen: es generalisiert Instruktionen nicht stillschweigend
von einem Beispiel auf andere und leitet keine ungefragten Requests ab — Vorteil für API-Use-Cases
mit sorgfältig getunten Prompts, aber Risiko, wenn man breite Anwendung erwartet ohne sie
explizit zu benennen. Standardmäßig bevorzugt Opus 4.8 Reasoning gegenüber Tool-Calls (meist
bessere Ergebnisse, aber ggf. zu wenig Tool-Nutzung in Wissensarbeit) und spawnt von sich aus
weniger Subagenten als man erwarten könnte — beides ist laut Doku über Effort bzw. explizite
Instruktion steuerbar.

## System-Prompt-Konventionen

Gemeinsame Anthropic-Techniken (Rolle geben, Klarheit/Direktheit, XML-Tags zur Strukturierung)
gelten laut der geteilten Best-Practices-Seite für alle aktuellen Claude-Modelle. Opus-4.8-
spezifisch: Verbosity-Kontrolle per Prompt, z. B.

> "Provide concise, focused responses. Skip non-essential context, and keep examples minimal."

Denken ("thinking") ist bei Opus 4.8 **standardmäßig aus**, bis explizit
`thinking: {type: "adaptive"}` gesetzt wird. Effort-Stufen laut Doku (Reihenfolge nach Einsatz):

| Effort | Einsatz laut Doku |
|---|---|
| `xhigh` | "the best setting for most coding and agentic use cases" |
| `high` | Minimum für die meisten intelligenz-sensitiven Use-Cases |
| `medium` | kostensensitiv, reduzierter Token-Verbrauch |
| `low` | kurze, latenzsensitive, nicht-intelligenz-sensitive Aufgaben |
| `max` | teils Gewinn, teils Overthinking — testen |

## Tool-Use & Agentic

Opus 4.8 "has a tendency to favor reasoning over tool calls" (produziert meist bessere
Ergebnisse) — höhere Effort-Stufen (`high`/`xhigh`) zeigen "substantially more tool usage in
agentic search and coding". Wenn das Modell erwartete Tools nicht aufruft, empfiehlt die Doku,
explizit im Prompt zu beschreiben, wann/wie es sie nutzen soll. Subagenten-Spawning ist steuerbar
über explizite Guidance dazu, wann Delegation angebracht ist. Die generische Anthropic-Tool-Use-
Doku (gilt familienweit): Client-Tools laufen in der eigenen Anwendung (`stop_reason: "tool_use"`
+ `tool_use`-Blocks, Antwort via `tool_result`), Server-Tools (`web_search`, `web_fetch`,
`code_execution`) laufen bei Anthropic. Default `tool_choice: {"type": "auto"}`; `strict: true`
in der Tool-Definition erzwingt exaktes Schema-Matching.

## Kontext & Länge

1M-Kontextfenster als Default bei der Migration von 4.7 (per `model-landscape.md` bestätigt:
alle aktuellen Opus-Varianten laufen mit 1M-Kontext). Bei `max`/`xhigh` Effort empfiehlt die Doku
ein großes `max_tokens`-Budget, damit für Denken + Subagenten-Koordination + Tool-Calls genug
Raum bleibt — Startwert **64k Tokens**, von dort aus tunen.

## Anti-Patterns

- **Breite Anwendung nicht implizit erwarten:** Opus 4.8 generalisiert eine Instruktion nicht
  automatisch auf ähnliche Fälle — Scope explizit benennen ("Apply this formatting to every
  section, not just the first one"), sonst bleibt es auf den literalen Einzelfall beschränkt.
- **Nicht auf altes Thinking-Default vertrauen:** Anders als evtl. erwartet ist Thinking
  standardmäßig aus; wer es braucht, muss `thinking: {type: "adaptive"}` aktiv setzen.
- **Nicht "um flaches Reasoning herumprompten":** Bei beobachtbar zu flachem Reasoning auf
  komplexen Aufgaben ist der richtige Hebel, die Effort-Stufe zu erhöhen (`high`/`xhigh`) —
  nicht zusätzliche Prompt-Tricks bei niedriger Effort-Stufe.
- **Alte Scaffolding-Zwischenmeldungen nicht beibehalten:** Wer bisher "After every 3 tool
  calls, summarize progress" erzwungen hat, sollte das entfernen — Opus 4.8 liefert von sich aus
  regelmäßige, hochwertige Progress-Updates.

## Beispiel-Snippets

System-Prompt-Skelett (Verbosity + explizite Scope-Klausel + Effort-Hinweis):

```
Provide concise, focused responses. Skip non-essential context, and keep examples minimal.

If an instruction should apply broadly, state the scope explicitly (e.g. "apply this to
every section, not just the first one") — do not assume it generalizes on its own.

This task involves multi-step reasoning. Think carefully through the problem before
responding.
```

Minimaler Tool-Use-Aufruf (aus der geteilten Anthropic-Doku, Server-Tool-Beispiel):

```python
client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-opus-4-8",
    max_tokens=1024,
    tools=[{"type": "web_search_20260209", "name": "web_search"}],
    messages=[{"role": "user", "content": "What's the latest on the Mars rover?"}],
)
print(response.content)
```
