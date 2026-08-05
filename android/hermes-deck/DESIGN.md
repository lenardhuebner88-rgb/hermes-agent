# Hermes Deck design language — "Violet on near-black"

Binding pattern doc for the `android/hermes-deck` app. This is **not** the
dashboard's design language: `web/src/control/DESIGN.md` describes a warm
graphite instrument panel for a desktop operator, and nothing in it transfers
here. Two different devices, two different postures, two different palettes.
Do not port tokens, spacing or component names across.

Canonical reference: the operator-supplied screenshot at
`~/.hermes/uploads/20260804-225323-27170.jpg` — a dark violet task board.
The tokens below already match it; what did not match, on the first read
(2026-08-05), was the *structure*. That gap is recorded in "Open gaps".

Mechanical binding of these tokens: `app/src/main/kotlin/net/hermes/deck/ui/Theme.kt`
(`DeckColors`, `DeckMetrics`). `Theme.kt` is the **one token source of truth**.
No raw `Color(0x…)` outside it.

## Tokens

Reached through `LocalDeck.current`, never through `MaterialTheme.colorScheme`
directly. The M3 scheme exists only so stock components (ripples, text
selection handles) do not fight the palette.

| Token | Value | Meaning / when to use |
|---|---|---|
| `background` | `#0B0711` | App canvas. Near-black with a violet cast, never neutral grey. |
| `surface` | `#17121F` | Card fill. Deliberately lighter than the ground. |
| `surfaceRaised` | `#1E1729` | Chips, inset tiles, the attachment squares. |
| `outline` | `#1AFFFFFF` | Hairline. A card is separated by a hairline, never a hard border. |
| `accent` | `#8B5CF6` | The one saturated colour. Interactive and current-state only. |
| `accentSoft` | `#2A1F45` | Fill behind a selected chip. |
| `textPrimary` | `#ECE9F1` | Titles, body, anything the eye reads first. |
| `textSecondary` | `#9B93AC` | Subtitles, metadata that supports a title. |
| `textFaint` | `#6B6479` | Section labels, timestamps, "nothing here" values. |
| `danger` / `warning` / `success` / `info` | `#F87171` / `#FBBF24` / `#34D399` / `#38BDF8` | Semantic only. Never decorative. |

Two brushes, both in `DeckColors`:

- `heroBrush` (`#241046 → #4C1D95`) — the hero card only. Two stops; a busier
  gradient reads as noise. Deliberately deeper than `accent`: a brighter first
  draft dominated the screen on-device and flattened everything below it.
- `accentBrush` (`#8B5CF6 → #A855F7`) — the primary button and the centre FAB.

The day strip is the one place `accent` fills a whole surface: the selected day
is solid `accent`, today carries an `accent` hairline, everything else is
`surface`. A tile's slot for its load keeps its height when empty — otherwise
only the days that carry a number grow and the strip comes out ragged.

`projectEdges` is a fixed list of eight; `edgeFor(key)` hashes a channel id
into it so a project keeps its colour across launches without storing
anything. The edge is a 3 dp bar on the leading side of a card — it is the
*only* place a non-accent hue appears as chrome.

## Geometry

`DeckMetrics`: `cardRadius` 24, `heroRadius` 28, `chipRadius` 14,
`screenPadding` 20, `cardPadding` 16, `gap` 12 (dp).

Large radii are the point. This is a phone held in one hand at arm's length;
tight corners read as a form, round ones as a card you can flick.

## Accent doctrine

1. **`accent` marks what is interactive or currently true** — the selected
   chip, the active nav item, the FAB, a non-zero open count. Never used to
   decorate a static surface.
2. **The hero card is the only large saturated area.** Exactly one per screen.
   If a second element wants the hero treatment, the screen has two subjects
   and should be split.
3. **Semantic colours are earned, not chosen.** `warning` means a state the
   user must resolve; `success` means a thing that completed. A colour picked
   because a row "needed some life" is a defect.

## Density doctrine — the rule this app keeps breaking

Every card shows **at most what fits without scrolling past it**. A Buzz
message is arbitrarily long: the approvals feed has carried a single message of
several thousand characters, and rendering it whole pushed everything below it
off the screen. That is the failure mode this app is most exposed to, because
its content comes from a channel it does not control.

So: **titles clamp to 2 lines, bodies to 3, and the card owns the truncation** —
never the sender. A card that cannot say what it is in five lines is a card
that needs a detail sheet, which every card here already has.

Clamping a card is necessary but not sufficient: a *section* of clamped cards
can still eat the screen. The first thing the deck shows must be the deck's own
content, not a queue of foreign messages ahead of it.

## Sheets

Bottom sheets are the app's only modal. `SheetScaffold` owns the geometry:
scrim, top radii 28 dp, `heightIn(max = 620.dp)`.

**A sheet must respect the keyboard.** `navigationBarsPadding()` and
`imePadding()` are not optional decoration — without them the sheet keeps its
full height *behind* the keyboard, and scrolling does not help, because the
content fits the sheet and the sheet is simply off-screen. On 2026-08-05 that
made the submit button of the capture sheet unreachable while typing.

**A sheet that submits must not dismiss before the work has settled.** The
share path finishes its host activity in `onDismiss`, and that cancels the
`viewModelScope` — dismissing first threw the note away silently, because the
sheet closed exactly as it does on success. Any new submitting sheet takes the
same shape: disable the button, show a present-tense label
("Wird abgelegt …"), dismiss in the completion callback.

## Empty states

An empty list says what is missing and what to do, in one line, in
`textSecondary` — "Noch keine Kanäle geladen — oben abgleichen". A list that
renders nothing at all is a defect: the user cannot tell an empty result from
a broken screen. This currently fails for search with no hits (see below).

## Language

German, informal, no exclamation marks. Labels are nouns
("Neu erfassen", "Anhänge"), buttons are verbs ("Ablegen", "Freigeben").
Section labels are `labelStyle` — 11 sp, semibold, 1 sp tracking, upper case,
`textFaint`. Timestamps are relative ("vor 8 Std."), never absolute.

## You may break a rule — say which and why

Every rule here may be broken; the one condition is that you name the rule and
justify the break in one sentence in the commit message. A broken rule with a
reason is a contribution; a silently circumvented one is a defect.

There is no mechanical gate for this document. `./gradlew test` covers the
model and wire layers only, and there are no instrumented tests — so the
enforcement is a live run on a device or emulator, and screenshots in the
commit trail. Every design claim in this file was made against a screenshot,
not against the source.

## Open gaps against the reference screenshot

Recorded 2026-08-05 against rendered screenshots, not against source and not
against accessibility dumps. The palette already matches; what is left is
structure and density.

**A note on how this list was made, because it went wrong once.** A first pass
read three gaps out of `uiautomator dump` output and all three were wrong: the
dump carries a card's *full* semantic text, so clamped cards look like text
walls, and it lists nodes that are merely below the fold, so present sections
look missing. A `screencap` corrected every one of them. Judge layout from the
picture; the dump is for coordinates.

1. **No sense of progress anywhere.** The reference's project cards carry a
   completion figure and member avatars; ours carry an open count and nothing
   else. There are no members in a Buzz channel to show, so this needs its own
   answer rather than a copy of the reference.
2. **Everything is one flat list.** No sorting, no grouping, no board columns,
   no bulk status change. All of it must stay expressible in `kind:9` plus
   tags, without touching a Buzz rule.
3. **A tapped attachment does nothing.** It has a thumbnail now but no full
   view.
4. **Agent names are short pubkeys.** `kind:0` profiles are never fetched.

Two earlier entries were **checked and removed**: the horizontal project row
exists (`HomeScreen`, below the approvals), and an empty search does render
"Nichts gefunden".

Closed on 2026-08-05, each verified on a device against real Buzz:

- **The day strip and due dates.** `DueStrip` carries the week with each day's
  open load; `DueChips` sets a date in two taps without a calendar modal; a
  passed date turns the card's metric `danger` and raises an overdue banner,
  because the strip starts today and cannot show what is already late.
- **The approvals section.** Two cards, two lines, action on the right — the
  project row and the task list are back above the fold.
- **Attachment previews.** Images render from Blossom through Coil, degrading
  to the icon when the tailnet is out of reach, which is the normal case off
  the network.
