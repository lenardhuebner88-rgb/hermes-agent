import { useState } from "react";
import { Send } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { de } from "../i18n/de";
import { dispatchOpenClawTask, type OpenClawDispatchBody } from "../hooks/useControlData";
import { ToneCallout } from "./atoms";

const AGENTS = ["atlas", "lens", "forge", "pixel"] as const;
type Agent = (typeof AGENTS)[number];

interface Props {
  /** Called after a successful dispatch so the parent can reload the list. */
  onDispatched?: () => void;
}

export function OpenClawDispatchForm({ onDispatched }: Props) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [agent, setAgent] = useState<Agent>("lens");
  const [deliverTo, setDeliverTo] = useState("");
  const [operatorLock, setOperatorLock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "emerald" | "red"; text: string } | null>(null);

  const requiresLock = agent === "pixel";

  const submit = async () => {
    const trimmed = title.trim();
    if (!trimmed) {
      setNotice({ tone: "red", text: de.openclaw.dispatchTitleRequired });
      return;
    }
    setBusy(true);
    setNotice(null);
    const body: OpenClawDispatchBody = { title: trimmed, agent };
    const desc = description.trim();
    if (desc) body.description = desc;
    const channel = deliverTo.trim();
    if (channel) body.deliver_to = channel;
    if (requiresLock) body.operator_lock_acknowledged = operatorLock;
    try {
      const result = await dispatchOpenClawTask(body);
      if (result.ok === false) {
        setNotice({ tone: "red", text: result.detail ?? de.openclaw.dispatchError });
      } else {
        setNotice({ tone: "emerald", text: de.openclaw.dispatchSuccess(result.taskId ?? "?") });
        setTitle("");
        setDescription("");
        setDeliverTo("");
        setOperatorLock(false);
        onDispatched?.();
      }
    } catch (e) {
      setNotice({ tone: "red", text: `${de.openclaw.dispatchError}: ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="hc-card p-4">
      <p className="hc-eyebrow">{de.openclaw.dispatchEyebrow}</p>
      <h3 className="mt-1 text-base font-semibold text-white">{de.openclaw.dispatchTitle}</h3>
      <p className="mt-1 text-sm hc-soft">{de.openclaw.dispatchHint}</p>

      <div className="mt-4 grid gap-3">
        <label className="grid gap-1 text-sm">
          <span className="hc-soft">{de.openclaw.dispatchTitleLabel}</span>
          <Input
            value={title}
            placeholder={de.openclaw.dispatchTitlePlaceholder}
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
        </label>

        <label className="grid gap-1 text-sm">
          <span className="hc-soft">{de.openclaw.dispatchDescriptionLabel}</span>
          <textarea
            className="min-h-[72px] rounded-lg border border-[var(--hc-border)] bg-transparent px-3 py-2 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]"
            value={description}
            placeholder={de.openclaw.dispatchDescriptionPlaceholder}
            onChange={(e) => setDescription(e.target.value)}
            disabled={busy}
          />
        </label>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="grid gap-1 text-sm">
            <span className="hc-soft">{de.openclaw.dispatchAgentLabel}</span>
            <Select value={agent} onValueChange={(v) => setAgent(v as Agent)} disabled={busy}>
              {AGENTS.map((a) => (
                <SelectOption key={a} value={a}>{a}</SelectOption>
              ))}
            </Select>
          </label>

          <label className="grid gap-1 text-sm">
            <span className="hc-soft">{de.openclaw.dispatchDeliverToLabel}</span>
            <Input
              value={deliverTo}
              placeholder={de.openclaw.dispatchDeliverToPlaceholder}
              inputMode="numeric"
              onChange={(e) => setDeliverTo(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>

        {requiresLock ? (
          <label className="flex items-center gap-2 text-sm text-amber-100">
            <input
              type="checkbox"
              checked={operatorLock}
              onChange={(e) => setOperatorLock(e.target.checked)}
              disabled={busy}
            />
            <span>{de.openclaw.dispatchOperatorLock}</span>
          </label>
        ) : null}

        {notice ? <ToneCallout tone={notice.tone}>{notice.text}</ToneCallout> : null}

        <div>
          <Button onClick={submit} disabled={busy} prefix={<Send className="h-4 w-4" />}>
            {busy ? de.openclaw.dispatchSubmitting : de.openclaw.dispatchSubmit}
          </Button>
        </div>
      </div>
    </section>
  );
}
