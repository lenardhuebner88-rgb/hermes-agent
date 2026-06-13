import { FleetPanel } from "../../components/fleet/atoms";
import { CopyButton } from "../backlog/CopyButton";
import type { PromptForgeCatalog } from "./catalog";

export function Kanon({ catalog }: { catalog: PromptForgeCatalog }) {
  return (
    <div className="grid gap-4">
      <FleetPanel eyebrow="12-Block-Taxonomie">
        <ul className="grid gap-2 text-sm">
          {catalog.blocks.map((b) => (
            <li key={b.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{b.letter} · {b.label}</span>
                <span className="hc-dim text-xs">{b.category}</span>
              </div>
              <p className="mt-1 hc-soft text-xs">{b.description}</p>
              <div className="mt-1.5 flex items-start justify-between gap-2">
                <code className="hc-mono whitespace-pre-wrap text-xs text-white/80">{b.body}</code>
                <CopyButton text={b.body} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 hc-dim text-[10px]">{b.source}</p>
            </li>
          ))}
        </ul>
      </FleetPanel>

      <FleetPanel eyebrow="Rohe Vorlagen (Kanon)">
        <div className="grid gap-3">
          {catalog.taskTypes.map((t) => (
            <div key={t.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{t.label}</span>
                <CopyButton text={t.rawTemplate} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <pre className="hc-mono mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 text-xs text-white/85">{t.rawTemplate}</pre>
              <p className="mt-1 hc-dim text-[10px]">{t.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Modus-Presets">
        <div className="grid gap-3">
          {catalog.modes.map((m) => (
            <div key={m.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{m.label}</span>
                <CopyButton text={m.rawPreset} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 hc-soft text-xs">{m.description}</p>
              <pre className="hc-mono mt-1.5 whitespace-pre-wrap rounded bg-black/30 p-2 text-xs text-white/85">{m.rawPreset}</pre>
              <p className="mt-1 hc-dim text-[10px]">{m.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Eval-Evidenz" meta={<span className="hc-dim text-xs">Scaffold ≫ Modell</span>}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="hc-dim text-left">
                <th className="py-1 pr-2">Eval</th>
                <th className="py-1 pr-2">misst</th>
                <th className="py-1 pr-2">belegte Zahl</th>
                <th className="py-1">Lehre</th>
              </tr>
            </thead>
            <tbody>
              {catalog.evalEvidence.map((e) => (
                <tr key={e.name} className="border-t border-white/5 align-top">
                  <td className="py-1.5 pr-2 font-medium text-white">{e.name}</td>
                  <td className="py-1.5 pr-2 hc-soft">{e.measures}</td>
                  <td className="py-1.5 pr-2 hc-mono text-white/85">{e.keyNumber}</td>
                  <td className="py-1.5 hc-soft">{e.lesson}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </FleetPanel>
    </div>
  );
}
