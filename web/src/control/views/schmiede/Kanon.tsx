import { FleetPanel } from "../../components/leitstand";
import { CopyButton } from "../backlog/CopyButton";
import type { PromptForgeCatalog } from "./catalog";

export function Kanon({ catalog }: { catalog: PromptForgeCatalog }) {
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <FleetPanel eyebrow="12-Baustein-Taxonomie">
        <ul className="grid grid-cols-1 gap-2 text-sm">
          {catalog.blocks.map((b) => (
            <li key={b.id} className="rounded-card border border-line-soft bg-surface-2 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-ink">{b.letter} · {b.label}</span>
                <span className="text-ink-3 text-xs">{b.category}</span>
              </div>
              <p className="mt-1 text-ink-2 text-xs">{b.description}</p>
              <div className="mt-1.5 flex items-start justify-between gap-2">
                <code className="font-data tabular-nums min-w-0 flex-1 whitespace-pre-wrap break-words rounded bg-surface-0 p-2 text-xs text-ink-2">{b.body}</code>
                <CopyButton text={b.body} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 text-ink-3 text-micro">{b.source}</p>
            </li>
          ))}
        </ul>
      </FleetPanel>

      <FleetPanel eyebrow="Rohe Vorlagen (Kanon)">
        <div className="grid grid-cols-1 gap-3">
          {catalog.taskTypes.map((t) => (
            <div key={t.id} className="rounded-card border border-line-soft bg-surface-2 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-ink">{t.label}</span>
                <CopyButton text={t.rawTemplate} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <pre className="font-data tabular-nums mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-0 p-2 text-xs text-ink-2">{t.rawTemplate}</pre>
              <p className="mt-1 text-ink-3 text-micro">{t.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Modus-Vorlagen">
        <div className="grid grid-cols-1 gap-3">
          {catalog.modes.map((m) => (
            <div key={m.id} className="rounded-card border border-line-soft bg-surface-2 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-ink">{m.label}</span>
                <CopyButton text={m.rawPreset} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 text-ink-2 text-xs">{m.description}</p>
              <pre className="font-data tabular-nums mt-1.5 whitespace-pre-wrap break-words rounded bg-surface-0 p-2 text-xs text-ink-2">{m.rawPreset}</pre>
              <p className="mt-1 text-ink-3 text-micro">{m.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Evaluationsbelege" meta={<span className="text-ink-3 text-xs">Gerüst ≫ Modell</span>}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="text-ink-3 text-left">
                <th className="py-1 pr-2">Eval</th>
                <th className="py-1 pr-2">misst</th>
                <th className="py-1 pr-2">belegte Zahl</th>
                <th className="py-1">Lehre</th>
              </tr>
            </thead>
            <tbody>
              {catalog.evalEvidence.map((e) => (
                <tr key={e.name} className="border-t border-line-soft align-top">
                  <td className="py-1.5 pr-2 font-medium text-ink">{e.name}</td>
                  <td className="py-1.5 pr-2 text-ink-2">{e.measures}</td>
                  <td className="py-1.5 pr-2 font-data tabular-nums text-ink-2">{e.keyNumber}</td>
                  <td className="py-1.5 text-ink-2">{e.lesson}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </FleetPanel>
    </div>
  );
}
