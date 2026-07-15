# Hermes Skill-Hygiene-Audit

## Ergebnis
- Aktive `SKILL.md`: **967** in **11** Wurzeln.
- Inventar je Wurzel:
  - `default`: 145
  - `profile:admin`: 110
  - `profile:coder`: 49
  - `profile:critic`: 102
  - `profile:family-ui`: 89
  - `profile:fo-brain`: 38
  - `profile:premium`: 91
  - `profile:research`: 96
  - `profile:reviewer`: 40
  - `profile:scout`: 96
  - `profile:verifier`: 111
- Frontmatter-Probleme: **2**; Name-vs-Verzeichnis-Abweichungen: **47**.
- Fehlende lokale Markdown-Link-Ziele: **35**.
- Skill-Namensduplikate/Profil-Forks: **122** (47 identisch, 75 divergent).
- Legacy-Textreferenzen je Typ: `atlas`=38, `coordinator`=38, `mission_control`=45, `openclaw`=88
- Redacted Muster: Secrets=api_key_assignment:87, bearer_token:20, provider_token_prefix:28, PII=email_address:284, phone_number:2132, Platzhalter=272

## Pruefregeln und Grenzen
- Pflichtfelder: `name`, `description`; YAML wird mit `yaml.safe_load` geparst.
- Aktiv bedeutet: SKILL.md unter den Wurzeln, mit Ausnahme jedes Pfads unter einer .archive-Komponente.
- Legacy-Hits sind textuelle treffer in aktiven skill.md; kein nachweis einer laufenden integration oder ausfuehrung.
- Link-Heuristik:
  - Geprueft werden nur Inline- und Referenz-Markdown-Links mit relativem Dateiziel.
  - HTTP(S), andere URI-Schemata, Anker, absolute Pfade, Home-Pfade und data:-Links werden bewusst uebersprungen.
  - Code-Fences, HTML-Attribute, dynamisch erzeugte Pfade und Link-Title-Syntax sind nicht vollstaendig geparst.
  - Es wird nur die Existenz geprueft; Link-Ziele werden nicht gelesen und Symlink-Semantik nicht bewertet.
- Redaction: Nur Musterarten und Anzahlen; keine Trefferwerte oder Fundstellen werden ausgegeben.

## Groesste Dateien nach Bytes

| Datei | Bytes | Zeilen |
| --- | ---: | ---: |
| `default/research/research-paper-writing/SKILL.md` | 105752 | 2377 |
| `profile:admin/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:critic/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:premium/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:research/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:scout/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:verifier/research/research-paper-writing/SKILL.md` | 103674 | 2377 |
| `profile:family-ui/research/research-paper-writing/SKILL.md` | 103375 | 2377 |
| `default/devops/hermes-agent-operations/SKILL.md` | 99619 | 573 |
| `profile:research/devops/openclaw-operator/SKILL.md` | 88143 | 987 |

## Groesste Dateien nach Zeilen

| Datei | Zeilen | Bytes |
| --- | ---: | ---: |
| `default/research/research-paper-writing/SKILL.md` | 2377 | 105752 |
| `profile:admin/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:critic/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:family-ui/research/research-paper-writing/SKILL.md` | 2377 | 103375 |
| `profile:premium/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:research/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:scout/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:verifier/research/research-paper-writing/SKILL.md` | 2377 | 103674 |
| `profile:reviewer/autonomous-ai-agents/hermes-agent/SKILL.md` | 1126 | 50424 |
| `profile:critic/autonomous-ai-agents/hermes-agent/SKILL.md` | 1111 | 51586 |
