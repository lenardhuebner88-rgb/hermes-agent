---
status: proposal
task: t_511ccac8
date: 2026-06-16
tags: [withings, family-organizer, design, gesundheit, tab]
---

# Withings-Backend → Family-Organizer-Tab — 3 Designvorschläge

**Frage:** Wie bekommen wir das fertige Withings-Backend (Gewicht / Schritte / Schlaf
aus den Withings-Geräten) als sichtbaren **Tab** in den Family Organizer?

**Kurzantwort:** Das Backend ist vollständig da und liefert Live-Daten — es gibt aber
**null Oberfläche** dafür. Unten drei Tab-Entwürfe, die sich in Umfang und Ziel-Bildschirm
unterscheiden (klein/privat → familien-nativ → ambient am Küchen-Display), plus die
Backend-Lücken, die *jeder* Tab vorher schließen muss. **Empfehlung:** Vorschlag **A** als
ersten lieferbaren Slice bauen, von Anfang an auf **B** ausgelegt; **C** später als Aufsatz.

---

## 1. Was wir heute schon haben (Backend-Inventar)

Das Withings-Backend lebt **nicht** im Hermes-Repo, sondern im Family Organizer
(`~/projects/family-organizer`). Es ist über vier Backlog-Slices (0194–0197) gebaut und
funktioniert — nur ohne UI.

**Endpunkte** (alle `GET`, alle hinter Basic-Auth/`/api/internal/`, also **nur server­seitig**
aufrufbar — der Browser darf Withings nie direkt rufen):

| Endpunkt | Liefert | Daten |
|---|---|---|
| `/api/internal/withings/health-summary` | Gewicht (`latestKg`, `trend7dKg`, Mess-Anzahl), Schritte gestern, Schlaf letzte Nacht (Dauer + Zeit im Bett) | **Live** von Withings, kein Cache |
| `/api/internal/withings/capability-probe` | „Was hat dieser Account überhaupt?" — Flags (Gewicht/Körperzusammensetzung/Schritte/Workouts/Schlaf/Herz) + Frische + **fertige UI-Empfehlungen** (`movementCard`, `sleepCard`, `weightCard`, `familyChallenge` je `ready`/`warmup`/`not_available`) | **Live**, mit transparentem Token-Refresh |
| `/api/internal/withings/connect?role=papa` | Startet echten OAuth-Flow pro Person (307-Redirect zu Withings) | — |
| `/api/auth/withings/callback` | OAuth-Rückkanal, prüft CSRF-State, tauscht & verschlüsselt Token | — (öffentlich, in Middleware ausgenommen) |
| `/api/internal/withings/demo-link` | Demo-Modus-Link (Withings-Sandbox) | — |

**Verfügbare Kennzahlen heute:** Gewicht inkl. 7-Tage-Trend, Schritte (gestern), Schlaf
(letzte Nacht). Körperzusammensetzung (Fett/Muskel) und Herzfrequenz sind im Account
*erkennbar* (capability-probe), werden von `health-summary` aber **noch nicht** ausgeliefert.

**Daten-Frische:** Voll live — jeder Aufruf macht 3 parallele HTTP-Calls zu Withings
(5 s Timeout je Call). Keine Tabelle mit Tageswerten, kein Cron, keine Webhooks. Tokens
liegen verschlüsselt (AES-256-GCM) in Supabase (`health_integration_tokens`).

**Mehr-Personen:** Der Token-Store ist bereits personen-fähig
(`storeWithingsTokenForFamilyMember` / `loadStoredWithingsTokenForFamilyMember`,
`token-store.ts:119,186`) und es gibt eine `health_profiles`-Tabelle pro Familienmitglied
(Migration 018). **Aber:** die Routen rufen noch den Single-User-Pfad (`loadStoredWithingsToken`,
`profileScoped:false`). Mehr-Personen ist im Speicher fertig, an den Endpunkten **nicht verdrahtet**.

**Bestehende UI:** **Keine.** Kein `/health`, keine Komponente, kein „Verbinden"-Button.
Der Connect-Flow wird heute nur ausgelöst, indem man den Browser von Hand auf
`/api/internal/withings/connect?role=papa` schickt.

---

## 2. Was *jeder* Tab vorher braucht (Backend-Lücken)

Unabhängig vom gewählten Entwurf sind das die Pflicht-Fixes:

1. **Token-Refresh in `health-summary`** — die Route nutzt `loadStoredWithingsToken`
   (ohne Auto-Refresh) und gibt nach ~3 h `access_token_expired` zurück. capability-probe
   refresht bereits korrekt (`loadUsableWithingsToken`, `token-store.ts:235`). Fix: gleichen
   Pfad in health-summary verwenden — sonst zeigt der Tab nach kurzer Zeit leere Werte.
   *(Belege: `health-summary/route.ts:21-31` vs. `capability-probe`.)*
2. **Personen-Routen** (nur für B/C) — `?role=`-Varianten von health-summary &
   capability-probe, die `loadStoredWithingsTokenForFamilyMember` rufen. Die Speicher-Seite
   existiert, die Route-Seite fehlt.
3. **Connect-/Verbinden-UI** — der `connect?role=`-Endpunkt braucht echte Knöpfe in der
   Oberfläche (heute Operator-only per URL).
4. **Server-seitig holen** — Tab = React Server Component, die `fetchWithingsHealthSummary`
   serverseitig ruft und Werte als Props an eine Client-Komponente reicht. Kein Client→Withings.
5. **Last-Sync / optionaler Cache** — `last_measure_sync_at` existiert in der Tabelle, wird
   aber nie geschrieben. Bei Mehr-Personen vervielfachen sich die Live-Calls (4× je 3 Calls,
   5 s Timeout) → entweder pro Mount nur capability-probe „warmziehen" und health-summary lazy,
   oder einen kleinen Tages-Snapshot einführen.

---

## 3. Wie ein Tab in FO überhaupt entsteht (Architektur)

FO nutzt den Next.js App-Router; Tabs sind Top-Level-Routen. Ein neuer „Gesundheit"-Tab
erfordert (in dieser Reihenfolge):

1. **Neu:** `src/app/gesundheit/page.tsx` — Server-Component, `export const dynamic = "force-dynamic"`,
   holt Daten via `Promise.all` über `loadWithFixtureFallback`-Loader (Muster wie
   `shopping/page.tsx`), reicht sie an eine `"use client"`-View.
2. **Neu:** `src/components/gesundheit/GesundheitView.tsx` — die Client-Ansicht (Personen-Wechsel,
   Karten).
3. **Edit:** `components/layout/BottomTabBar.tsx:6` — Eintrag + `grid-cols-5`→`grid-cols-6` (Z.30).
4. **Edit:** `components/layout/AppShell.tsx:15` — in `shellTabItems` + `calmShellRoutes` (Z.23-34).
5. **Edit:** `components/layout/Sidebar.tsx:8` — Typ-Union + Array (Legacy-Konsistenz).

**Design-Sprache:** Tailwind v4 mit `design/tokens.css` (kein shadcn). Warme Papier-Töne
(`bg-panel`/`bg-card`), Terra-Akzent (`text-terra`), Fraunces-Serife für große Zahlen
(`font-display`), ruhige Karten `rounded-md border border-rule bg-panel`, Sektions-Label
`text-[11px] font-bold uppercase tracking-[0.18em]`. Es gibt **keine** fertige `<StatCard>` —
die wird neu gebaut (Vorlage: `TodayHeroCard.tsx`).

**Personen-Modell:** `FamilyMember` (`papa`/`mama`/`kind-1`/`kind-2`), geladen via
`listFamilyMembers()`. `MemberAvatar` (farbiger Ring + Initiale/Foto) existiert; einen
globalen Personen-Umschalter gibt es **nicht** — Muster zum Nachbauen: der Avatar-Tap-Picker
in `KitchenQuickAdd.tsx:154`.

---

## 4. Die drei Vorschläge

### Vorschlag A — „Gesundheit" als ruhige Ein-Personen-Tab (MVP)

> Der kleinstmögliche echte Tab. Ein Account (der bereits verdrahtete Single-User-Pfad),
> drei ruhige Karten, capability-probe steuert, was sichtbar ist.

```
┌────────────────────────────────────────────┐
│  Gesundheit                      [Verbinden]│
│                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Gewicht  │ │ Bewegung │ │  Schlaf  │     │
│  │  82,4 kg │ │  8.214   │ │  7 h 12  │     │
│  │ ▾ 0,6 kg │ │ Schritte │ │ letzte   │     │
│  │  (7 Tage)│ │ gestern  │ │ Nacht    │     │
│  └──────────┘ └──────────┘ └──────────┘     │
└────────────────────────────────────────────┘
```

- **Was der Nutzer sieht:** Drei Karten (Gewicht + 7-Tage-Trend, Schritte gestern, Schlaf
  letzte Nacht). capability-probe blendet Karten aus, die der Account nicht hat, und zeigt
  „Aufwärmphase"/„noch keine Daten" statt leerer Felder. Ist nichts verbunden → großer
  „Withings verbinden"-Button.
- **Datenquelle:** capability-probe (zum Gaten) + health-summary (Werte), beide unverändert
  außer dem Token-Refresh-Fix (Lücke #1).
- **Aufwand:** klein. 1 Route + 1 View + 4 Nav-Edits + Fix #1. **Keine** Personen-Verdrahtung.
- **Pro:** schnell lieferbar, geringes Risiko, macht den Connect-Flow erstmals bedienbar,
  passt zur ruhigen Design-Sprache. **Contra:** zeigt nur *einen* Account — widerspricht dem
  Familien-Kern von FO (jedes Mitglied hat ggf. ein eigenes Withings-Konto).

---

### Vorschlag B — „Familie & Gesundheit": Mehr-Personen-Tab mit Avatar-Umschalter

> Der familien-native Entwurf. Gleiche Karten wie A, aber oben eine Avatar-Reihe als
> Personen-Wähler — pro Mitglied ein eigener Withings-Account.

```
┌────────────────────────────────────────────┐
│  Gesundheit                                  │
│  (Papa) (Mama) (Oskar) (Fiete)               │
│   ●▔▔▔                                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Gewicht  │ │ Bewegung │ │  Schlaf  │     │
│  │  82,4 kg │ │  8.214   │ │  7 h 12  │     │
│  └──────────┘ └──────────┘ └──────────┘     │
│  Mama: ○ noch nicht verbunden → [Verbinden]  │
└────────────────────────────────────────────┘
```

- **Was der Nutzer sieht:** Avatar-Reihe (`MemberAvatar`) als Tabs; Klick wechselt die Person.
  Pro Person dieselben Karten wie A. Nicht-verbundene Mitglieder zeigen ihren eigenen
  „Verbinden"-Knopf (`connect?role=<member>`).
- **Datenquelle:** **erfordert Lücke #2** — `?role=`-Varianten von health-summary &
  capability-probe, die `loadStoredWithingsTokenForFamilyMember` nutzen. Server holt alle
  Mitglieder per `Promise.all`, Client wählt aus.
- **Aufwand:** mittel. Wie A plus: 2 personen-fähige Routen, Avatar-Switcher (Muster aus
  `KitchenQuickAdd`), Connect-pro-Person-UI. Mehr Live-Calls → Timeout/Frische beachten (Lücke #5).
- **Pro:** der „richtige" Langzeit-Entwurf, trifft das Familien-Modell, macht Connect je
  Person nutzbar. **Contra:** größer; Vervielfachung der Withings-Calls braucht Sorgfalt
  (lazy laden / kleiner Snapshot).

---

### Vorschlag C — „Familien-Challenge": ambient am Küchen-Display, Detail im Tab

> Privatsphäre-bewusster Aufsatz. Statt nur einer privaten Health-Seite kommt das
> *Gemeinschaftliche* (Bewegung, Schritt-Challenge) ambient auf das immer sichtbare
> Küchen-Tablet; das Private (Gewicht) bleibt im Detail-Tab.

```
Küchen-Board (/kitchen, immer an):
┌───────────────────────── … ──────────────────┐
│  Heute                          Familien-Schritte │
│  …Wochenplan…                   👟 24.806 heute   │
│                                 Papa ▓▓▓ Oskar ▓▓ │
└───────────────────────────────────────────────┘
   ↳ Tippen → /gesundheit (Detail je Person, wie B)
```

- **Was der Nutzer sieht:** Auf dem Küchen-Display ein kompaktes Bewegungs-/Familien-Schritt-
  Widget, getrieben von `uiRecommendation.familyChallenge` (`ready`/`wait_for_7_days`).
  Gewicht erscheint **nicht** am gemeinsamen Bildschirm (`weightCard: private_ready`), nur im
  persönlichen Tab. Der Tab (= Vorschlag B) ist die Drill-Down-Detailebene.
- **Datenquelle:** setzt B (Mehr-Personen) voraus, plus Aggregation für die Familien-Summe;
  nutzt die `familyChallenge`-Empfehlung, die das Backend schon berechnet.
- **Aufwand:** hoch. B + Board-Widget im fixen 1100px-No-Scroll-Layout + Aggregation +
  Privatsphäre-Gating.
- **Pro:** höchster „ambient"-Wert für ein Familien-Küchen-Display, nutzt vorhandene
  Backend-Empfehlung, privatsphäre-bewusst. **Contra:** der größte Brocken; sinnvoll erst,
  wenn Mehr-Personen (B) steht.

---

## 5. Empfehlung

**A → B → C als inkrementeller Pfad. Jetzt mit A starten, von Anfang an auf B ausgelegt.**

- **A jetzt:** liefert sofort sichtbaren Wert, behebt nebenbei den Token-Refresh-Bug (Lücke #1),
  macht den Connect-Flow erstmals klickbar — bei minimalem Diff und Risiko.
- **B als Ziel:** FO *ist* eine Familien-App; der Ein-Personen-Tab ist nur die ehrliche
  Zwischenstufe. Die View aus A so bauen, dass die Avatar-Reihe und die `?role=`-Routen
  später ohne Umbau andocken.
- **C optional:** der ambient-Küchen-Aufsatz lohnt sich, sobald B steht und mehrere
  Familienmitglieder verbunden sind — sonst zeigt das Board eine leere Challenge.

Wenn nur *eine* Sache gebaut werden soll: **A**. Wenn der Tab gleich „fertig" wirken soll und
ein bisschen mehr Aufwand ok ist: **direkt B**.

---

## 6. Quellen (Datei:Zeile)

**Backend (FO):** `src/app/api/internal/withings/health-summary/route.ts:6,21-31` ·
`.../capability-probe/route.ts:16` · `.../connect/route.ts:20` · `.../demo-link/route.ts:15` ·
`src/app/api/auth/withings/callback/route.ts:17` · `src/middleware.ts:94` ·
`src/lib/integrations/withings/client.ts:3,42-54,370` · `.../capability-probe.ts:10-43` ·
`.../token-store.ts:119,186,235` · `supabase/migrations/017_health_integration_tokens`,
`018_withings_health_profiles` · `backlog/items/0194-0197-withings-*`.

**FO-Tab-Architektur:** `src/app/shopping/page.tsx` (Daten-Muster) · `src/app/kitchen/page.tsx:11` ·
`src/components/layout/AppShell.tsx:15,23-34,58-91` · `.../BottomTabBar.tsx:6,30` ·
`.../Sidebar.tsx:8` · `design/tokens.css` · `src/components/kitchen/TodayHeroCard.tsx:70` ·
`.../MemberAvatar.tsx` · `.../KitchenQuickAdd.tsx:154` · `src/lib/kitchen-data/types.ts:33` ·
`src/lib/integrations/supabase/server.ts:1086`.

---

*Recherche-Deliverable zu Task `t_511ccac8`. Reines Design/Research — kein Code geändert.
Filing-Hinweis: Original in `docs/design/` (hermes-agent), Kopie in der Bibliothek-Receipts-Regal.*
