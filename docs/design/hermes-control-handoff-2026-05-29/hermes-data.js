/* ============================================================
   Hermes Control — geteilte Demo-Daten + Helfer
   Echtes Datenmodell 1:1 (Feldnamen & Status-Enums wie im Vertrag).
   Wird von allen drei Richtungen geladen.  Global: window.HERMES
   ============================================================ */
(function () {
  // Fixer Referenz-Zeitpunkt, damit Alters-/Laufzeit-Anzeigen deterministisch sind.
  // 29/05/2026 (Demo-Referenzzeit)
  const NOW = 1780041720; // epoch seconds
  const m = (min) => min * 60;
  const h = (hr) => hr * 3600;

  /* ── HERMES-WORKER ── GET /api/plugins/kanban/workers/active ───────────── */
  // Ein Worker = ein laufender Prozess. Felder exakt aus dem Vertrag.
  const hermesWorkers = [
    {
      run_id: "run_9f3a21", task_id: "T-4821",
      task_title: "Diff-Renderer im Autoresearch-UI refaktorieren",
      task_status: "running", task_assignee: "hermes", profile: "coder",
      worker_pid: 21884, started_at: NOW - h(2) - m(14), claim_lock: "lock_4821",
      claim_expires: NOW + m(46), last_heartbeat_at: NOW - 8,
      max_runtime_seconds: h(4),
      run_status: "running", run_outcome: null,
      inspect: { cpu_percent: 41.6, rss: 536870912, num_threads: 9, num_fds: 64, status: "running", alive: true },
    },
    {
      run_id: "run_7c1d04", task_id: "T-4830",
      task_title: "Quellen zur Tailscale-ACL-Härtung sammeln",
      task_status: "running", task_assignee: "hermes", profile: "research",
      worker_pid: 21992, started_at: NOW - m(31), claim_lock: "lock_4830",
      claim_expires: NOW + h(1) + m(29), last_heartbeat_at: NOW - 3,
      max_runtime_seconds: h(2),
      run_status: "running", run_outcome: null,
      inspect: { cpu_percent: 11.8, rss: 293601280, num_threads: 6, num_fds: 48, status: "running", alive: true },
    },
    {
      run_id: "run_2b8e55", task_id: "T-4835",
      task_title: "Queue nach Priorität neu sortieren",
      task_status: "running", task_assignee: "hermes", profile: "dispatcher",
      worker_pid: 22010, started_at: NOW - m(4), claim_lock: "lock_4835",
      claim_expires: NOW + m(56), last_heartbeat_at: NOW - 1,
      max_runtime_seconds: h(1),
      run_status: "running", run_outcome: null,
      inspect: { cpu_percent: 4.3, rss: 121634816, num_threads: 4, num_fds: 31, status: "running", alive: true },
    },
    {
      run_id: "run_5a4402", task_id: "T-4799",
      task_title: "Kanban-Schema auf v3 migrieren",
      task_status: "blocked", task_assignee: "hermes", profile: "devpower",
      worker_pid: 21640, started_at: NOW - h(1) - m(2), claim_lock: "lock_4799",
      claim_expires: NOW + m(18), last_heartbeat_at: NOW - m(4),
      max_runtime_seconds: h(3),
      run_status: "blocked", run_outcome: "blocked",
      block_reason: "Wartet auf Migrations-Lock von run_9f3a21 (DB-Schema)",
      inspect: { cpu_percent: 0.2, rss: 184549376, num_threads: 5, num_fds: 40, status: "sleeping", alive: true },
    },
    {
      run_id: "run_3d77aa", task_id: "T-4788",
      task_title: "Review: PR #482 — Retry-Logik",
      task_status: "review", task_assignee: "hermes", profile: "critic",
      worker_pid: 21501, started_at: NOW - m(52), claim_lock: "lock_4788",
      claim_expires: NOW - m(7), /* abgelaufen! */ last_heartbeat_at: NOW - 94, /* alt! */
      max_runtime_seconds: h(1),
      run_status: "running", run_outcome: null,
      inspect: { cpu_percent: 0.0, rss: 207618048, num_threads: 5, num_fds: 38, status: "running", alive: true },
    },
    {
      run_id: "run_1e09c2", task_id: "T-4702",
      task_title: "Nächtliche Aufräum-Routine für archivierte Runs",
      task_status: "running", task_assignee: "hermes", profile: "admin",
      worker_pid: 20984, started_at: NOW - h(3) - m(58), claim_lock: "lock_4702",
      claim_expires: NOW - m(40), last_heartbeat_at: NOW - m(11),
      max_runtime_seconds: h(4),
      run_status: "timed_out", run_outcome: "timed_out",
      block_reason: "Laufzeit-Budget erschöpft — Prozess reagiert nicht mehr",
      inspect: { cpu_percent: 0.0, rss: 98566144, num_threads: 3, num_fds: 22, status: "zombie", alive: false },
    },
  ];

  // Profil-Rollen (Klartext) für Anzeige
  const profileLabels = {
    default: "Standard", admin: "Admin", coder: "Coder", devpower: "DevPower",
    dispatcher: "Dispatcher", kanbanops: "Kanban-Ops", planner: "Planer",
    research: "Research", critic: "Kritiker",
  };

  /* ── OPENCLAW-AGENTEN ── GET /api/openclaw/agents ──────────────────────── */
  const openclawAgents = [
    {
      id: "main", name: "Main", emoji: "🦅", status: "active",
      model: "claude-opus-4.6", lastActive: NOW - 5,
      roleLabel: "Orchestrator", roleSummary: "Verteilt Arbeit, hält den Gesamtplan",
      stuckSignal: false, activityPulse: 0.92,
      tasks: {
        queued: [{ id: "o-12", title: "Sprint-Plan KW22 schnüren", priority: "med", progressPercent: 0 }],
        active: [{ id: "o-09", title: "Flotte auf Diff-Feature einschwören", priority: "high", progressPercent: 64 }],
        review: [],
        recentDone: [{ id: "o-03", title: "Tagesziele verteilt", priority: "med", progressPercent: 100 }],
      },
      fleetHealth: { currentTask: "Flotte auf Diff-Feature einschwören", heartbeat: NOW - 5, throughput: "8 Tasks/h", currentTool: "dispatch", lastOutput: "3 Tasks an frontend-guru übergeben" },
      escalationNote: null,
    },
    {
      id: "sre-expert", name: "SRE-Expert", emoji: "🔧", status: "monitoring",
      model: "claude-sonnet-4.6", lastActive: NOW - 22,
      roleLabel: "SRE / Infra", roleSummary: "Wacht über Fehler-Budget & Deploys",
      stuckSignal: false, activityPulse: 0.55,
      tasks: {
        queued: [],
        active: [{ id: "o-21", title: "Error-Budget nach Deploy beobachten", priority: "high", progressPercent: 40 }],
        review: [{ id: "o-18", title: "Alert-Regel ‚disk-90%‘ gegenprüfen", priority: "med", progressPercent: 90 }],
        recentDone: [{ id: "o-11", title: "Rollback v2.3.1 verifiziert", priority: "high", progressPercent: 100 }],
      },
      fleetHealth: { currentTask: "Error-Budget nach Deploy beobachten", heartbeat: NOW - 22, throughput: "2 Tasks/h", currentTool: "grafana", lastOutput: "p99-Latenz stabil bei 180ms" },
      escalationNote: null,
    },
    {
      id: "frontend-guru", name: "Frontend-Guru", emoji: "🎨", status: "active",
      model: "claude-opus-4.6", lastActive: NOW - 3,
      roleLabel: "UI", roleSummary: "Baut & poliert die Oberfläche",
      stuckSignal: false, activityPulse: 0.88,
      tasks: {
        queued: [{ id: "o-31", title: "Hell-Modus für Vorschlags-Cards", priority: "low", progressPercent: 0 }],
        active: [{ id: "o-27", title: "Diff-Einklappen auf dem Handy", priority: "high", progressPercent: 72 }],
        review: [],
        recentDone: [{ id: "o-22", title: "Bottom-Tab-Bar Safe-Area gefixt", priority: "med", progressPercent: 100 }, { id: "o-19", title: "Skeleton-States ergänzt", priority: "low", progressPercent: 100 }],
      },
      fleetHealth: { currentTask: "Diff-Einklappen auf dem Handy", heartbeat: NOW - 3, throughput: "5 Tasks/h", currentTool: "editor", lastOutput: "DiffView.tsx — 2 Dateien geändert" },
      escalationNote: null,
    },
    {
      id: "efficiency-auditor", name: "Efficiency-Auditor", emoji: "🔍", status: "ready",
      model: "claude-haiku-4.6", lastActive: NOW - m(6),
      roleLabel: "Kosten / Audit", roleSummary: "Findet teure & redundante Läufe",
      stuckSignal: false, activityPulse: 0.18,
      tasks: {
        queued: [{ id: "o-41", title: "Token-Verbrauch KW21 auswerten", priority: "med", progressPercent: 0 }],
        active: [], review: [],
        recentDone: [{ id: "o-33", title: "3 doppelte Cron-Jobs gemeldet", priority: "med", progressPercent: 100 }],
      },
      fleetHealth: { currentTask: "—", heartbeat: NOW - m(6), throughput: "0 Tasks/h", currentTool: "—", lastOutput: "Bereit, wartet auf Audit-Fenster" },
      escalationNote: null,
    },
    {
      id: "james", name: "James", emoji: "🔬", status: "active",
      model: "claude-opus-4.6", lastActive: NOW - m(7),
      roleLabel: "Research", roleSummary: "Tiefenrecherche & Quellenarbeit",
      stuckSignal: true, activityPulse: 0.05,
      tasks: {
        queued: [],
        active: [{ id: "o-52", title: "Vergleich: Vektor-DBs für Memory", priority: "high", progressPercent: 35 }],
        review: [], recentDone: [],
      },
      fleetHealth: { currentTask: "Vergleich: Vektor-DBs für Memory", heartbeat: NOW - m(7), throughput: "0 Tasks/h", currentTool: "browser", lastOutput: "Seit 7 Min. keine Ausgabe — hängt evtl. an Quelle" },
      escalationNote: "Kein Fortschritt seit 7 Min — Tool-Aufruf prüfen oder neu anstoßen.",
    },
    {
      id: "spark", name: "Spark", emoji: "🪄", status: "offline",
      model: "claude-sonnet-4.6", lastActive: NOW - h(2) - m(40),
      roleLabel: "Relief", roleSummary: "Springt bei Lastspitzen ein",
      stuckSignal: false, activityPulse: 0,
      tasks: { queued: [], active: [], review: [], recentDone: [{ id: "o-08", title: "Lastspitze 07:10 abgefangen", priority: "med", progressPercent: 100 }] },
      fleetHealth: { currentTask: "—", heartbeat: NOW - h(2) - m(40), throughput: "0 Tasks/h", currentTool: "—", lastOutput: "Offline — kein Heartbeat seit 2h 40m" },
      escalationNote: null,
    },
  ];

  /* ── AUTORESEARCH ── GET /autoresearch/status (autoresearch-runner-status-v1) ── */
  const autoresearchStatus = {
    state: "running",            // 'idle' | 'running' | 'stopping' | 'crashed'
    pid: 24117,
    request_id: "ar_2026-05-29_0931",
    iteration: 3, max: 8,
    last_step: "Diff erzeugt für Skill ‚findmy‘ (Abschnitt Output)",
    last_eval: "eval grün — Skill lädt, Beispiele konsistent",
    route_status: "configured",
    heartbeat_age_s: 4, heartbeat_fresh: true,
    last_receipt: "rcpt_0931_findmy",
    last_run: NOW - m(1),
    note: "Loop sucht nach kleinen, sicheren Verbesserungen. 3 Vorschläge warten auf dich.",
  };

  // Vorschläge — GET /autoresearch/proposals. diff_before_after als Zeilen-Array
  // für konsistentes Rendering (type: 'ctx' | 'add' | 'del').
  const proposals = [
    {
      id: "p-001", mode: "skill", status: "proposed",
      target: "Skill: findmy", section: "Output",
      title: "Fügt Abschnitt ‚Output‘ zum Skill ‚findmy‘ hinzu",
      rationale_plain: "Der Skill sagt bisher nicht, in welchem Format das Ergebnis zurückkommt. Mit einem klaren ‚Output‘-Abschnitt muss kein Worker mehr raten.",
      diff_before_after: [
        { type: "ctx", text: "## Schritte" },
        { type: "ctx", text: "1. Standort über die iCloud-API abfragen" },
        { type: "ctx", text: "2. Gerät anhand der ID auflösen" },
        { type: "add", text: "" },
        { type: "add", text: "## Output" },
        { type: "add", text: "Gib das Ergebnis als JSON zurück:" },
        { type: "add", text: "- `device`  — Name des Geräts" },
        { type: "add", text: "- `lat`, `lon`  — Koordinaten" },
        { type: "add", text: "- `accuracy_m`  — Genauigkeit in Metern" },
        { type: "add", text: "- `seen_at`  — Zeitstempel (ISO 8601)" },
      ],
    },
    {
      id: "p-002", mode: "skill", status: "proposed",
      target: "Skill: tailscale-acl", section: "Schritte",
      title: "Präzisiert Schritt 3 im Skill ‚tailscale-acl‘",
      rationale_plain: "Schritt 3 sagte nur ‚Tag setzen‘ — aber nicht welchen. Jetzt steht der konkrete Tag und die Regel, die danach zu prüfen ist.",
      diff_before_after: [
        { type: "ctx", text: "## Schritte" },
        { type: "ctx", text: "1. Neues Gerät in der Admin-Konsole bestätigen" },
        { type: "ctx", text: "2. Gerät benennen (schema: worker-NN)" },
        { type: "del", text: "3. Tag setzen und speichern" },
        { type: "add", text: "3. Tag `tag:worker` setzen" },
        { type: "add", text: "4. ACL-Regel `tag:worker → tag:kanban` prüfen, dann speichern" },
      ],
    },
    {
      id: "p-003", mode: "code", status: "proposed",
      target: "src/autoresearch/runner.py", section: "heartbeat_age()",
      title: "Fängt fehlenden Heartbeat im Status-Code ab",
      rationale_plain: "Wenn ein Heartbeat-Wert fehlt, stürzt die Statusanzeige ab. Diese Änderung behandelt den Fall sauber, statt zu crashen.",
      diff_before_after: [
        { type: "ctx", text: "def heartbeat_age(self, hb):" },
        { type: "del", text: "    return time.time() - hb" },
        { type: "add", text: "    if hb is None:" },
        { type: "add", text: "        return None" },
        { type: "add", text: "    return time.time() - hb" },
      ],
    },
    {
      id: "p-101", mode: "skill", status: "applied",
      target: "Skill: kanbanops", section: "Abschluss",
      title: "Macht die Abschluss-Anweisung im Skill ‚kanbanops‘ eindeutig",
      rationale_plain: "‚Bewege die Task‘ war unklar. Jetzt steht genau wohin und unter welcher Bedingung.",
      result: "✓ übernommen — Skill: eval grün",
      applied_at: NOW - m(14),
      diff_before_after: [
        { type: "ctx", text: "## Abschluss" },
        { type: "del", text: "Bewege die Task wenn sie fertig ist." },
        { type: "add", text: "Verschiebe die Task nach Spalte `done`, sobald alle Checks grün sind." },
      ],
    },
    {
      id: "p-102", mode: "code", status: "applied",
      target: "src/plugins/kanban/workers.py", section: "dispatch-retry",
      title: "Staffelt Wiederholungen nach Timeout (Backoff)",
      rationale_plain: "Nach einem Timeout hämmerte der Worker sofort neu. Jetzt wartet er gestaffelt und entlastet das System.",
      result: "✓ übernommen — Code: Tests grün (14/14)",
      applied_at: NOW - m(38),
      diff_before_after: [
        { type: "ctx", text: "for attempt in range(max_retries):" },
        { type: "ctx", text: "    try:" },
        { type: "ctx", text: "        return dispatch(task)" },
        { type: "ctx", text: "    except TimeoutError:" },
        { type: "del", text: "        continue" },
        { type: "add", text: "        time.sleep(min(2 ** attempt, 30))" },
      ],
    },
    {
      id: "p-103", mode: "skill", status: "skipped",
      target: "Skill: critic", section: "Review-Checkliste",
      title: "Verschärft die Review-Checkliste im Skill ‚critic‘",
      rationale_plain: "Schlägt zwei zusätzliche Prüfpunkte für Reviews vor.",
      result: "übersprungen",
      diff_before_after: [
        { type: "ctx", text: "## Review-Checkliste" },
        { type: "ctx", text: "- Tests vorhanden?" },
        { type: "add", text: "- Fehlerpfade getestet?" },
        { type: "add", text: "- Keine offenen TODOs im Diff?" },
      ],
    },
  ];

  const activityLog = [
    { at: NOW - m(1),  text: "Diff für Skill ‚findmy‘ erzeugt — wartet auf deine Freigabe", tone: "violet" },
    { at: NOW - m(14), text: "Skill ‚kanbanops‘ übernommen — eval grün", tone: "emerald" },
    { at: NOW - m(22), text: "Eval gestartet für Vorschlag ‚tailscale-acl‘", tone: "zinc" },
    { at: NOW - m(38), text: "Code ‚workers.py‘ übernommen — Tests grün (14/14)", tone: "emerald" },
    { at: NOW - m(51), text: "Vorschlag ‚critic‘ übersprungen (vom Betreiber)", tone: "zinc" },
    { at: NOW - h(1),  text: "Loop gestartet — Runde 1 von 8", tone: "zinc" },
  ];

  /* ── Sekundär-Navigation (Drawer / „Mehr") ─────────────────────────────── */
  const moreNav = [
    { id: "sessions", label: "Sessions", icon: "history" },
    { id: "kanban",   label: "Kanban-Board", icon: "kanban" },
    { id: "modelle",  label: "Modelle", icon: "cpu" },
    { id: "logs",     label: "Logs", icon: "scroll-text" },
    { id: "cron",     label: "Cron", icon: "timer" },
    { id: "skills",   label: "Skills", icon: "puzzle" },
    { id: "config",   label: "Konfiguration", icon: "settings-2" },
  ];

  /* ── Helfer ────────────────────────────────────────────────────────────── */
  // kurzes Alter ab epoch-Sekunden: "3s","4m","2h","4d"
  function fmtAge(epochSec) {
    const d = Math.max(0, NOW - epochSec);
    if (d < 60) return d + "s";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  }
  // Dauer aus Sekunden: "2h 14m" / "4m" / "52s"
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(sec));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60);
    if (hh > 0) return hh + "h " + String(mm).padStart(2, "0") + "m";
    if (mm > 0) return mm + "m";
    return sec + "s";
  }
  function fmtMB(bytes) { return Math.round(bytes / 1048576) + " MB"; }

  // Worker-Gesundheit ableiten (stuck/blocked/timed_out/healthy)
  function workerHealth(w) {
    const hbAge = NOW - w.last_heartbeat_at;
    const expired = w.claim_expires < NOW;
    if (w.run_status === "timed_out" || w.run_status === "crashed" || !w.inspect.alive)
      return { key: "offline", tone: "zinc", label: "Offline", dot: "offline" };
    if (w.run_status === "blocked")
      return { key: "blocked", tone: "red", label: "Blockiert", dot: "error" };
    if (hbAge > 90 || expired)
      return { key: "stuck", tone: "amber", label: "Stuck", dot: "warn" };
    return { key: "healthy", tone: "cyan", label: "Läuft", dot: "live" };
  }

  // Tone je Agent-Status
  const agentStatusTone = {
    active: "cyan", monitoring: "amber", ready: "sky", idle: "zinc", offline: "zinc",
  };
  const agentStatusLabel = {
    active: "Aktiv", monitoring: "Beobachtet", ready: "Bereit", idle: "Inaktiv", offline: "Offline",
  };
  // Agent-Farb-Token
  const agentColorVar = {
    main: "--agent-atlas", "sre-expert": "--agent-forge", "frontend-guru": "--agent-pixel",
    "efficiency-auditor": "--agent-lens", james: "--agent-james", spark: "--agent-spark",
  };

  const priorityLabel = { high: "Hoch", med: "Mittel", low: "Niedrig" };
  const priorityTone  = { high: "rose", med: "amber", low: "zinc" };

  const taskStatusLabel = {
    triage: "Triage", todo: "Offen", scheduled: "Geplant", ready: "Bereit",
    running: "Läuft", blocked: "Blockiert", review: "Review", done: "Fertig", archived: "Archiv",
  };

  // Aggregierte Übersicht („Ist alles gesund?")
  function overview() {
    const hHealthy = hermesWorkers.filter((w) => workerHealth(w).key === "healthy").length;
    const hProblem = hermesWorkers.filter((w) => ["stuck", "blocked", "offline"].includes(workerHealth(w).key));
    const ocHealthy = openclawAgents.filter((a) => ["active", "monitoring", "ready"].includes(a.status) && !a.stuckSignal).length;
    const ocProblem = openclawAgents.filter((a) => a.stuckSignal || a.status === "offline");
    const openProps = proposals.filter((p) => p.status === "proposed").length;
    const warnings = [
      ...hProblem.map((w) => ({ kind: "hermes", w, health: workerHealth(w) })),
      ...ocProblem.map((a) => ({ kind: "openclaw", a })),
    ];
    return {
      hermesTotal: hermesWorkers.length, hermesHealthy: hHealthy,
      hermesRunning: hermesWorkers.filter((w) => w.run_status === "running").length,
      ocTotal: openclawAgents.length, ocHealthy: ocHealthy,
      ocActive: openclawAgents.filter((a) => a.status === "active").length,
      openProps, autoState: autoresearchStatus.state, autoFresh: autoresearchStatus.heartbeat_fresh,
      warnings,
      allHealthy: warnings.length === 0,
    };
  }

  window.HERMES = {
    NOW, hermesWorkers, openclawAgents, autoresearchStatus, proposals, activityLog, moreNav,
    profileLabels, agentStatusTone, agentStatusLabel, agentColorVar,
    priorityLabel, priorityTone, taskStatusLabel,
    fmtAge, fmtDur, fmtMB, workerHealth, overview,
    fmtClock(epochSec) {
      const d = new Date(epochSec * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
  };
})();
