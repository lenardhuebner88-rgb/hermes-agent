import { Shield } from "lucide-react";
import { ToneCallout } from "../components/atoms";

export function OpenClawPlaceholder() {
  return (
    <section className="hc-card space-y-4 p-5">
      <div className="grid h-12 w-12 place-items-center rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"><Shield className="h-6 w-6" /></div>
      <div><p className="hc-eyebrow">B3 zurückgestellt</p><h2 className="mt-1 text-xl font-semibold text-white">OpenClaw kommt bald</h2><p className="mt-2 max-w-2xl hc-soft">Der Tab bleibt sichtbar, aber ohne Read-only-Proxy bauen wir hier keine Scheinsteuerung.</p></div>
      <ToneCallout tone="amber">Keine Mutationen, kein Proxy-Neubau in diesem Slice.</ToneCallout>
    </section>
  );
}
