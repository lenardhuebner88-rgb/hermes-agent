import { useEffect, useState } from "react";
import { Settings2 } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { fetchJSON } from "@/lib/api";
import type { AuxiliaryModelsResponse, ModelOptionsResponse } from "@/lib/api";
import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import { de } from "../../i18n/de";
import { StatusPill, ToneCallout } from "../../components/atoms";
import { Panel, SkeletonCard, Text } from "../../components/primitives";

type LaneModelSlot = "skills_hub" | "code_audit" | "test_hardening";

const LANE_MODEL_SLOTS: readonly { task: LaneModelSlot; lane: string; label: string; hint: string }[] = [
  { task: "skills_hub", lane: "Skill+Code", label: "Skills Hub", hint: "Skill- und Code-Lane" },
  { task: "code_audit", lane: "Deep-Audit", label: "Code Audit", hint: "Deep-Audit-Lane" },
  { task: "test_hardening", lane: "Test-Foundry", label: "Test Hardening", hint: "Test-Foundry-Lane" },
];

export function LaneModelPanel() {
  const [aux, setAux] = useState<AuxiliaryModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pickerTask, setPickerTask] = useState<LaneModelSlot | null>(null);
  const [savingTask, setSavingTask] = useState<LaneModelSlot | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadAux = async () => {
    setLoading(true);
    setError(null);
    try {
      setAux(await fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadAux());
  }, []);

  const assignmentFor = (task: LaneModelSlot) => aux?.tasks.find((item) => item.task === task) ?? null;
  const pickerSlot = pickerTask ? LANE_MODEL_SLOTS.find((slot) => slot.task === pickerTask) ?? null : null;

  const loadOptionsForPicker = async (): Promise<ModelOptionsResponse> => {
    const options = await fetchJSON<ModelOptionsResponse>("/api/model/options");
    const current = pickerTask ? assignmentFor(pickerTask) : null;
    if (!current?.provider || current.provider === "auto") return options;
    return {
      ...options,
      provider: current.provider,
      model: current.model,
      providers: options.providers?.map((provider) => ({ ...provider, is_current: provider.slug === current.provider })),
    };
  };

  return (
    <Panel eyebrow={de.autoresearch.laneModelsEyebrow} title={de.autoresearch.laneModelsHeading} actions={loading ? <Spinner /> : null} className="sm:p-5">
      {error ? <ToneCallout tone="red">{de.autoresearch.laneModelsFailed}: {error}</ToneCallout> : null}
      {loading && !aux ? <SkeletonCard rows={2} className="mb-3" /> : null}
      <div className="grid gap-3 md:grid-cols-3">
        {LANE_MODEL_SLOTS.map((slot) => {
          const assignment = assignmentFor(slot.task);
          const isAuto = !assignment?.provider || assignment.provider === "auto";
          const value = isAuto ? de.autoresearch.laneModelAuto : `${assignment?.provider}${assignment?.model ? ` · ${assignment.model}` : ""}`;
          return (
            <div key={slot.task} className="rounded-lg border border-white/10 bg-white/[.03] p-3">
              <div className="mb-2 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <Text as="p" variant="label" className="flex items-center gap-1.5 text-white"><Settings2 className="h-3.5 w-3.5" />{slot.lane}</Text>
                  <p className="mt-1 text-xs hc-dim">{slot.hint}</p>
                </div>
                <StatusPill tone={isAuto ? "zinc" : "cyan"} label={isAuto ? "Auto" : slot.label} />
              </div>
              <p className="hc-mono min-h-5 truncate text-xs hc-soft" title={value}>{value}</p>
              <Button outlined className="hc-hit mt-3 w-full" onClick={() => setPickerTask(slot.task)} disabled={loading || !!savingTask} prefix={savingTask === slot.task ? <Spinner /> : <Settings2 className="h-4 w-4" />}>
                {de.autoresearch.laneModelChange}
              </Button>
            </div>
          );
        })}
      </div>
      {pickerTask && pickerSlot ? (
        <ModelPickerDialog
          key={`${pickerTask}-${refreshKey}`}
          loader={loadOptionsForPicker}
          alwaysGlobal
          title={de.autoresearch.laneModelPickerTitle(pickerSlot.lane)}
          onApply={async ({ provider, model }) => {
            setSavingTask(pickerTask);
            try {
              await fetchJSON<unknown>("/api/model/set", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope: "auxiliary", task: pickerTask, provider, model }),
              });
              await loadAux();
              setRefreshKey((value) => value + 1);
            } finally {
              setSavingTask(null);
            }
          }}
          onClose={() => setPickerTask(null)}
        />
      ) : null}
    </Panel>
  );
}

export function CodeAuditSlotPicker() {
  return <SingleLaneModelPicker task="code_audit" titleLane="Deep-Audit" />;
}

export function TestHardeningSlotPicker() {
  return <SingleLaneModelPicker task="test_hardening" titleLane="Test-Foundry" />;
}

function SingleLaneModelPicker({ task, titleLane }: { task: "code_audit" | "test_hardening"; titleLane: string }) {
  const [aux, setAux] = useState<AuxiliaryModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadAux = async () => {
    setLoading(true);
    try {
      setAux(await fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadAux());
  }, []);

  const assignment = aux?.tasks.find((item) => item.task === task) ?? null;
  const value = !assignment?.provider || assignment.provider === "auto" ? de.autoresearch.laneModelAuto : `${assignment.provider}${assignment.model ? ` · ${assignment.model}` : ""}`;

  const loadOptionsForPicker = async (): Promise<ModelOptionsResponse> => {
    const options = await fetchJSON<ModelOptionsResponse>("/api/model/options");
    if (!assignment?.provider || assignment.provider === "auto") return options;
    return {
      ...options,
      provider: assignment.provider,
      model: assignment.model,
      providers: options.providers?.map((provider) => ({ ...provider, is_current: provider.slug === assignment.provider })),
    };
  };

  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-xs hc-soft">Modell</span>
        {loading ? <Spinner /> : <StatusPill tone={!assignment?.provider || assignment.provider === "auto" ? "zinc" : "cyan"} label={task} />}
      </div>
      <p className="hc-mono truncate text-xs hc-soft" title={value}>{value}</p>
      <Button outlined className="hc-hit mt-2 w-full" onClick={() => setPickerOpen(true)} disabled={loading || saving} prefix={saving ? <Spinner /> : <Settings2 className="h-4 w-4" />}>
        {de.autoresearch.laneModelChange}
      </Button>
      {pickerOpen ? (
        <ModelPickerDialog
          key={`${task}-${refreshKey}`}
          loader={loadOptionsForPicker}
          alwaysGlobal
          title={de.autoresearch.laneModelPickerTitle(titleLane)}
          onApply={async ({ provider, model }) => {
            setSaving(true);
            try {
              await fetchJSON<unknown>("/api/model/set", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope: "auxiliary", task, provider, model }),
              });
              await loadAux();
              setRefreshKey((value) => value + 1);
            } finally {
              setSaving(false);
            }
          }}
          onClose={() => setPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}
