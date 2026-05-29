/**
 * Deutscher String-Katalog. Eine Quelle für UI-Copy — hält die Operator-Tonalität
 * (terse, Klartext, „du"-Imperative, kein Jargon) an einem Ort. Auch bei nur einer
 * Sprache lohnt das: Copy-Reviews und spätere i18n werden trivial.
 *
 * Tonalität: Operator-zu-Operator. Knapp, handlungsorientiert. Kein „wir".
 */
export const de = {
  app: { name: 'Hermes Control', operator: 'pieter_pan' },

  tabs: {
    overview: 'Übersicht',
    hermes: 'Hermes',
    openclaw: 'OpenClaw',
    autoresearch: 'Autoresearch',
    more: 'Mehr',
  },

  overview: {
    healthyTitle: 'Alles läuft ruhig.',
    healthySub: 'Beide Flotten melden sich regelmäßig, der Autoresearch-Loop arbeitet im Hintergrund.',
    warnTitle: (n: number) => `${n} ${n === 1 ? 'Signal wartet' : 'Signale warten'} auf dich.`,
    warnSub: 'Tippe einen Punkt an, um direkt zur Stelle zu springen.',
    eyebrowHealthy: 'System nominal',
    eyebrowWarn: 'Eingriff empfohlen',
    needsAttention: 'Braucht Aufmerksamkeit',
    nothingUrgent: 'Nichts Dringendes. Alle Worker melden sich.',
    stat: { hermes: 'Hermes-Worker', openclaw: 'OpenClaw aktiv', proposals: 'Vorschläge offen', warnings: 'Warnungen' },
  },

  worker: {
    runtime: 'Laufzeit', heartbeat: 'Heartbeat', remaining: 'Rest-Zeit', process: 'Prozess',
    healthy: 'Läuft', stuck: 'Stuck', blocked: 'Blockiert', offline: 'Offline',
    actions: { inspect: 'Inspect', details: 'Details', dispatch: 'Dispatch', nudge: 'Anstoßen', unlock: 'Lock lösen', restart: 'Neu starten' },
    stuckReason: (age: string) => `Heartbeat ${age} alt · claim_expires überschritten`,
    offlineReason: 'Prozess reagiert nicht mehr',
  },

  agent: {
    currentTask: 'Aktuelle Aufgabe',
    queue: { waiting: 'Wartet', active: 'Aktiv', review: 'Review', done: 'Fertig' },
    lastOutput: 'Letzte Ausgabe',
  },

  autoresearch: {
    nextStep: 'Dein nächster Schritt',
    nextStepOpen: (n: number) => `${n} ${n === 1 ? 'Verbesserung ist' : 'Verbesserungen sind'} geprüft und ${n === 1 ? 'wartet' : 'warten'}. Lies das „Warum", schau dir den Diff an, übernimm mit einem Klick.`,
    nextStepEmpty: 'Alles abgearbeitet. Hol dir neue Vorschläge, wenn du willst.',
    fetchMore: 'Verbesserungen holen',
    applyAll: 'Alle übernehmen',
    apply: 'Übernehmen',
    skip: 'Überspringen',
    proposals: 'Vorschläge',
    done: 'Erledigt',
    activity: 'Aktivität',
    why: 'Warum',
    beforeAfter: 'Vorher / Nachher',
    codeBadge: 'Code-Änderung',
    skillBadge: 'Skill',
    codeGate: 'Höhere Stufe — wird erst nach grüner Test-Suite scharf geschaltet.',
    skipped: 'übersprungen',
    resultSkill: '✓ übernommen — Skill: eval grün',
    resultCode: '✓ übernommen — Code: Tests grün',
    loopRunning: 'Loop läuft',
    idle: 'Idle',
  },

  command: {
    placeholder: 'Befehl, Worker oder Vorschlag suchen…',
    palettePlaceholder: 'Springe zu… oder tippe einen Befehl',
    noMatch: 'Kein Treffer',
    groups: { nav: 'Navigation', more: 'Mehr', actions: 'Aktionen', hermes: 'Hermes-Worker', openclaw: 'OpenClaw' },
  },

  tweaks: {
    title: 'Tweaks', accent: 'Akzent', density: 'Dichte', appearance: 'Erscheinung',
    airy: 'Luftig', compact: 'Kompakt', dark: 'Dunkel', light: 'Hell',
    pulse: 'Heartbeat-Puls', kbdHints: 'Tastatur-Hinweise', on: 'An', off: 'Aus',
  },
} as const;

export type Dict = typeof de;
