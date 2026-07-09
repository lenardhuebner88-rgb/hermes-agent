---
title: "Prompting-Guide: GPT-5.x & Codex"
type: model
tags:
  - llm-wiki
  - prompting
family: gpt5-codex
model_ids:
  - openai/gpt-5
  - openai/gpt-5-chat
  - openai/gpt-5-codex
  - openai/gpt-5-image
  - openai/gpt-5-image-mini
  - openai/gpt-5-mini
  - openai/gpt-5-nano
  - openai/gpt-5-pro
  - openai/gpt-5.1
  - openai/gpt-5.1-chat
  - openai/gpt-5.1-codex
  - openai/gpt-5.1-codex-max
  - openai/gpt-5.1-codex-mini
  - openai/gpt-5.2
  - openai/gpt-5.2-chat
  - openai/gpt-5.2-codex
  - openai/gpt-5.2-pro
  - openai/gpt-5.3-chat
  - openai/gpt-5.3-codex
  - openai/gpt-5.4
  - openai/gpt-5.4-image-2
  - openai/gpt-5.4-mini
  - openai/gpt-5.4-nano
  - openai/gpt-5.4-pro
  - openai/gpt-5.5
  - openai/gpt-5.5-pro
  - openai/gpt-5.6-luna
  - openai/gpt-5.6-luna-pro
  - openai/gpt-5.6-sol
  - openai/gpt-5.6-sol-pro
  - openai/gpt-5.6-terra
  - openai/gpt-5.6-terra-pro
updated: 2026-07-09
maturity: curated
sources:
  - https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_prompting_guide
  - https://learn.chatgpt.com/docs/agent-configuration/agents-md
  - https://learn.chatgpt.com/docs/prompting
  - https://developers.openai.com/api/docs/guides/function-calling
---

# Prompting-Guide: GPT-5.x & Codex

Diese Guide deckt die **gesamte GPT-5.x-Familie** ab: die GA-Chat-Linie (`gpt-5` … `gpt-5.6-sol/
-terra/-luna`, inkl. `-pro`/`-mini`/`-nano`/`-fast`-Varianten) und die codex-gebrandeten Agenten-
Varianten (`gpt-5-codex` … `gpt-5.3-codex`). Grund: OpenAIs eigene ID-Präfixe trennen "codex" nur
namentlich, das GPT-5-Prompting-Verhalten (Agentic Eagerness, Tool-Calling, Reasoning-Effort) ist
laut dem offiziellen GPT-5-Prompting-Guide generationsweit beschrieben, nicht pro Branding-
Variante. **Quelle fehlt:** ein Dokument, das GA-Chat- und Codex-Branding-Varianten *innerhalb*
GPT-5.x prompting-technisch trennt, wurde nicht gefunden — Codex-spezifisches unten bezieht sich
auf das Codex-*Produkt* (CLI/IDE/Cloud), nicht auf ein einzelnes Modell-ID-Suffix.

## Stärken & Schwächen

Laut OpenAIs GPT-5-Prompting-Guide markiert GPT-5 "a substantial leap forward in agentic task
performance, coding, raw intelligence, and steerability". Das Modell ist trainiert, um über das
gesamte Spektrum von Autonomie zu operieren — von selbstständigem Entscheiden unter Ambiguität
bis zu eng geführten, gut definierten Aufgaben ("agentic eagerness" ist explizit steuerbar, siehe
unten). Für agentische/Tool-Calling-Workflows empfiehlt OpenAI ausdrücklich, auf die **Responses
API** zu wechseln, weil dort Reasoning zwischen Tool-Calls persistiert wird ("more efficient and
intelligent outputs") statt bei jedem Call neu abgeleitet zu werden.

## System-Prompt-Konventionen

Für das **Codex-Produkt** (CLI/IDE/Cloud) ist die maßgebliche "System-Prompt"-Konvention nicht
der klassische `system`-Turn, sondern **`AGENTS.md`**: Codex liest es vor jeder Arbeit. Laut
offizieller Doku baut Codex bei jedem Start eine Instruction-Chain nach fester Präzedenz:

> "Global scope: […] Codex reads `AGENTS.override.md` if it exists. Otherwise, Codex reads
> `AGENTS.md`. Codex uses only the first non-empty file at this level. Project scope: Starting
> at the project root […] it checks for `AGENTS.override.md`, then `AGENTS.md` […] Merge order:
> Codex concatenates files from the root down […] Files closer to your current directory
> override earlier guidance because they appear later in the combined prompt."

Wichtig: pro Verzeichnis wird **nur eine** Datei eingebunden (Override verdrängt Base, beide
werden nicht gemergt), und die Gesamtgröße ist per `project_doc_max_bytes` gedeckelt (Default
32 KiB) — bei Überschreitung splitten oder Limit erhöhen.

Für die GA-Chat-/API-Linie (kein Codex-Produkt) ist die "System-Prompt-Konvention" die
Steuerung der Agentic Eagerness per `reasoning_effort` + expliziten XML-artigen Tags im
System-Prompt (siehe Snippets).

## Tool-Use & Agentic

Agentic Eagerness ist laut Guide in beide Richtungen steuerbar. Für **weniger** Eagerness
(schnellere, fokussiertere Antworten): niedrigeren `reasoning_effort` wählen und/oder ein
`<context_gathering>`-Tag mit expliziten Stop-Kriterien einsetzen (siehe Snippets). Für **mehr**
Eagerness/Autonomie (weniger Rückfragen, mehr Persistenz bis zur Lösung): höheren
`reasoning_effort` und ein `<persistence>`-Tag einsetzen. Zusätzlich unterstützt GPT-5
"tool preambles" — steuerbare Zwischenmeldungen über anstehende Tool-Calls, die bei langen
Traces die User Experience verbessern.

Für generisches Function-Calling (OpenAI-API, gilt auch als De-facto-Standard, den DeepSeek/Kimi
kompatibel nachbilden): Tools werden als `{"type": "function", "function": {name, description,
parameters, strict}}` deklariert; der Round-Trip läuft über fünf Schritte — Request mit Tools →
Tool-Call vom Modell → eigene Ausführung → zweiter Request mit Tool-Output → finale Antwort.

## Kontext & Länge

Alle aktuellen GPT-5.6-Varianten (Sol/Terra/Luna, inkl. `-pro`) laufen laut `model-landscape.md`
mit 1M-Kontextfenster. Für agentische Pipelines mit vielen Tool-Calls empfiehlt OpenAI die
Responses API statt Chat Completions, weil Reasoning-State zwischen Calls persistiert wird statt
bei jedem Turn neu im Kontext mitgeschleppt zu werden.

## Anti-Patterns

- **Agentic Eagerness ungetunt lassen:** GPT-5 ist standardmäßig gründlich/umfassend beim
  Context-Gathering — ohne explizite Stop-Kriterien kann das bei latenzsensitiven Workflows zu
  Overexploration führen.
- **AGENTS.md-Präzedenz missverstehen:** ein Override in einem Unterverzeichnis **verdrängt**
  die Base-Datei am selben Level, statt sie zu ergänzen — wer beides braucht, muss die Base-
  Regeln explizit in die Override-Datei kopieren.
- **Fallback-Dateinamen nicht ohne Registrierung erwarten:** ein Repo mit z. B. `TEAM_GUIDE.md`
  statt `AGENTS.md` wird ignoriert, bis der Name in `project_doc_fallback_filenames` eingetragen
  ist.
- **Bei agentischen Pipelines auf Chat Completions statt Responses API bestehen:** verschenkt
  laut Doku Effizienz- und Intelligenzgewinne aus dem persistierten Reasoning-State.

## Beispiel-Snippets

`~/.codex/AGENTS.md` (globale Defaults, wörtlich aus der Doku):

```markdown
# ~/.codex/AGENTS.md
## Working agreements
- Always run `npm test` after modifying JavaScript files.
- Prefer `pnpm` when installing dependencies.
- Ask for confirmation before adding new production dependencies.
```

`<context_gathering>`-Tag zur Reduktion von Agentic Eagerness (wörtlich aus dem GPT-5-Guide):

```xml
<context_gathering>
Goal: Get enough context fast. Parallelize discovery and stop as soon as you can act.
Method:
- Start broad, then fan out to focused subqueries.
- In parallel, launch varied queries; read top hits per query. Deduplicate paths and
  cache; don't repeat queries.
Early stop criteria:
- You can name exact content to change.
- Top hits converge (~70%) on one area/path.
Loop:
- Batch search → minimal plan → complete task.
- Search again only if validation fails or new unknowns appear.
Prefer acting over more searching.
</context_gathering>
```

`<persistence>`-Tag zur Erhöhung von Autonomie (wörtlich aus dem GPT-5-Guide):

```xml
<persistence>
- You are an agent - please keep going until the user's query is completely resolved,
  before ending your turn and yielding back to the user.
- Only terminate your turn when you are sure that the problem is solved.
- Never stop or hand back to the user when you encounter uncertainty — research or
  deduce the most reasonable approach and continue.
- Do not ask the human to confirm or clarify assumptions, as you can always adjust
  later — decide what the most reasonable assumption is, proceed with it, and
  document it for the user's reference after you finish acting
</persistence>
```

Function-Calling-Tool-Definition (wörtlich aus der OpenAI-Doku):

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_horoscope",
        "description": "Get today's horoscope for an astrological sign.",
        "parameters": {
            "type": "object",
            "properties": {
                "sign": {"type": "string", "description": "An astrological sign like Taurus or Aquarius"},
            },
            "required": ["sign"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}]
```
