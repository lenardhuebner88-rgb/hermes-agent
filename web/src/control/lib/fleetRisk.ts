import type { ReliabilityProfile, ReliabilityResponse, LanesCatalogResponse } from "./schemas";
import type { PressureStatusResponse, SystemHealthResponse, ToneName } from "./types";

type MinimalCatalogProfile = Pick<LanesCatalogResponse["profiles"][number], "name">;

const NOISE_PROFILE_NAMES = new Set(["", "unbekannt", "unknown", "default", "admin", "test-worker"]);
const KNOWN_OPERATIONAL_PROFILES = new Set(["coder", "reviewer", "verifier", "scout", "premium", "critic", "research"]);

export interface ReliabilityRiskRow {
  profile: string;
  runs: number;
  completedPct: number | null;
  failedPct: number | null;
  retryPct: number | null;
  retries: number;
  tone: ToneName;
  sampleLabel: string | null;
  notable: boolean;
}

export interface ReliabilityRiskModel {
  rows: ReliabilityRiskRow[];
  hiddenCount: number;
  lowSampleHiddenCount: number;
  noiseHiddenCount: number;
  notableCount: number;
  windowLabel: string;
  summary: string;
  defaultOpen: boolean;
}

export function formatReliabilityWindow(hours: number | null | undefined): string {
  const safeHours = Math.max(1, Math.round(hours ?? 168));
  if (safeHours % 24 === 0) {
    const days = safeHours / 24;
    return days === 1 ? "letzte 24 Stunden" : `letzte ${days} Tage`;
  }
  return `letzte ${safeHours} Stunden`;
}

function pct(value: number | null | undefined): number | null {
  return value == null ? null : Math.round(value * 100);
}

function cleanProfile(profile: string | null | undefined): string {
  return String(profile ?? "").trim();
}

function isNoiseProfile(profile: string): boolean {
  const lower = profile.toLowerCase();
  return NOISE_PROFILE_NAMES.has(lower) || lower.length <= 1 || lower.includes("test");
}

export function riskToneForReliability(input: {
  completed_rate: number | null;
  failed_rate: number | null;
  retry_rate: number | null;
}): ToneName {
  const completed = input.completed_rate ?? 1;
  const failed = input.failed_rate ?? 0;
  const retry = input.retry_rate ?? 0;
  if (failed >= 0.2 || completed < 0.6 || retry >= 0.4) return "red";
  if (failed >= 0.05 || completed < 0.8 || retry >= 0.25) return "amber";
  return "emerald";
}

function isOperationalProfile(
  profile: string,
  catalogNames: Set<string>,
  activeNames: Set<string>,
): boolean {
  if (isNoiseProfile(profile)) return false;
  return catalogNames.has(profile) || activeNames.has(profile) || KNOWN_OPERATIONAL_PROFILES.has(profile);
}

function toneRank(tone: ToneName): number {
  if (tone === "red") return 2;
  if (tone === "amber") return 1;
  return 0;
}

function rowFromProfile(profile: ReliabilityProfile, activeNames: Set<string>): ReliabilityRiskRow {
  const tone = riskToneForReliability(profile);
  const notable = tone === "amber" || tone === "red";
  const activeLowSample = activeNames.has(profile.profile) && (profile.low_sample || profile.runs < 5);
  return {
    profile: profile.profile,
    runs: profile.runs,
    completedPct: pct(profile.completed_rate),
    failedPct: pct(profile.failed_rate),
    retryPct: pct(profile.retry_rate),
    retries: profile.retries,
    tone,
    sampleLabel: activeLowSample ? "aktiv, wenig Daten" : null,
    notable,
  };
}

export function buildReliabilityRiskModel(input: {
  reliability: ReliabilityResponse | null;
  laneCatalogProfiles: MinimalCatalogProfile[];
  activeWorkerProfiles: string[];
}): ReliabilityRiskModel {
  const reliability = input.reliability;
  const allProfiles = reliability?.profiles ?? [];
  const minN = Math.max(1, reliability?.min_n ?? 5);
  const catalogNames = new Set(input.laneCatalogProfiles.map((p) => cleanProfile(p.name)).filter(Boolean));
  const activeNames = new Set(input.activeWorkerProfiles.map(cleanProfile).filter(Boolean));
  let lowSampleHiddenCount = 0;
  let noiseHiddenCount = 0;

  const rows = allProfiles
    .filter((profile) => {
      const name = cleanProfile(profile.profile);
      const active = activeNames.has(name);
      if (!active && (profile.low_sample || profile.runs < minN)) {
        lowSampleHiddenCount += 1;
        return false;
      }
      if (!isOperationalProfile(name, catalogNames, activeNames)) {
        noiseHiddenCount += 1;
        return false;
      }
      return true;
    })
    .map((profile) => rowFromProfile({ ...profile, profile: cleanProfile(profile.profile) }, activeNames))
    .sort((a, b) => Number(b.notable) - Number(a.notable) || toneRank(b.tone) - toneRank(a.tone) || b.runs - a.runs || a.profile.localeCompare(b.profile, "de"));

  const notableCount = rows.filter((row) => row.notable).length;
  const windowLabel = formatReliabilityWindow(reliability?.since_hours);
  const hiddenCount = lowSampleHiddenCount + noiseHiddenCount;
  const summaryPrefix = notableCount > 0 ? `${notableCount} auffaellig` : "ruhig";
  return {
    rows,
    hiddenCount,
    lowSampleHiddenCount,
    noiseHiddenCount,
    notableCount,
    windowLabel,
    summary: `${summaryPrefix} · ${rows.length} Profile · ${windowLabel}`,
    defaultOpen: notableCount > 0,
  };
}

export interface SystemPulseRiskRow {
  key: "gateway" | "dispatcher" | "pressure" | "token" | "host";
  label: string;
  value: string;
  detail: string | null;
  tone: ToneName;
}

export interface SystemPulseRiskModel {
  headline: string;
  overallTone: ToneName;
  rows: SystemPulseRiskRow[];
}

function healthTone(status: string | null | undefined): ToneName {
  if (status === "healthy") return "emerald";
  if (status === "degraded") return "amber";
  return "red";
}

function pressureTone(status: string | null | undefined, recommendationTone?: ToneName): ToneName {
  if (recommendationTone === "amber" || recommendationTone === "red") return recommendationTone;
  if (status === "saturated") return "red";
  if (status === "busy" || status === "unknown") return "amber";
  return "emerald";
}

function heartbeatValue(age: number | null | undefined, fallback: string): string {
  return age == null ? fallback : `vor ${Math.round(age)} s`;
}

function tokenPressureValue(token: PressureStatusResponse["token_pressure"] | undefined): string {
  if (!token) return "-";
  return token.pct == null ? token.class : `${token.class} · ${Math.round(token.pct)} %`;
}

export function buildSystemPulseRiskModel(input: {
  systemHealth: SystemHealthResponse | null;
  pressureStatus: PressureStatusResponse | null;
}): SystemPulseRiskModel {
  const gateway = input.systemHealth?.subsystems.gateway;
  const dispatcher = input.systemHealth?.subsystems.kanban_dispatcher;
  const pressure = input.pressureStatus;
  const pressureRowTone = pressureTone(pressure?.overall, pressure?.recommendation.tone);
  const systemTone = healthTone(input.systemHealth?.overall);
  const overallTone: ToneName = pressureRowTone === "red" || systemTone === "red"
    ? "red"
    : pressureRowTone === "amber" || systemTone === "amber"
      ? "amber"
      : "emerald";
  const hostCpu = pressure?.host.cpu_percent == null ? "-" : `${Math.round(pressure.host.cpu_percent)} %`;
  const hostRam = pressure?.host.memory_percent == null ? "-" : `${Math.round(pressure.host.memory_percent)} %`;

  return {
    overallTone,
    headline: `System ${input.systemHealth?.overall === "healthy" ? "ok" : input.systemHealth?.overall ?? "unklar"} · ${pressure?.recommendation.label ?? "Pressure unklar"}`,
    rows: [
      {
        key: "gateway",
        label: "Gateway",
        value: heartbeatValue(gateway?.heartbeat_age_s, gateway?.status ?? "-"),
        detail: gateway?.error ?? gateway?.detail ?? null,
        tone: healthTone(gateway?.status),
      },
      {
        key: "dispatcher",
        label: "Kanban-Dispatcher",
        value: heartbeatValue(dispatcher?.heartbeat_age_s, dispatcher?.status ?? "-"),
        detail: dispatcher?.error ?? dispatcher?.detail ?? null,
        tone: healthTone(dispatcher?.status),
      },
      {
        key: "pressure",
        label: "Pressure",
        value: pressure?.recommendation.label ?? pressure?.overall ?? "-",
        detail: pressure?.recommendation.detail ?? pressure?.cause ?? null,
        tone: pressureRowTone,
      },
      {
        key: "token",
        label: "Token-Pressure",
        value: tokenPressureValue(pressure?.token_pressure),
        detail: pressure?.token_pressure.updated_at == null ? null : "Runtime-Status",
        tone: pressure?.token_pressure.class === "ok" || pressure?.token_pressure.class === "normal" ? "emerald" : "amber",
      },
      {
        key: "host",
        label: "Host",
        value: `${hostCpu} CPU · ${hostRam} RAM`,
        detail: pressure?.host.load_avg.length ? `Load ${pressure.host.load_avg.slice(0, 2).map((v) => v.toFixed(1)).join(" / ")}` : null,
        tone: pressure?.overall === "saturated" ? "red" : pressure?.overall === "busy" ? "amber" : "zinc",
      },
    ],
  };
}
