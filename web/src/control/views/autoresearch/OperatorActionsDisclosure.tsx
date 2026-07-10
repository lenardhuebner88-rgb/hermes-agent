import { Archive, ArrowDown, FlaskConical, GitPullRequestArrow, RotateCw, SearchCode, ShieldCheck, Sparkles } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { codeWeaknessBusyKey } from "../../lib/autoresearch";
import type { getAutoresearchActionPlan } from "../../lib/autoresearchActionPlan";
import { de } from "../../i18n/de";
import { Disclosure } from "../../components/primitives";
import { OperatorActionCard } from "./panels";

type ActionPlan = ReturnType<typeof getAutoresearchActionPlan>;

export function OperatorActionsDisclosure({
  actionPlan,
  storeBusy,
  openSkillCount,
  openSkillManualReviewCount,
  canApplyAllOpenSkills,
  pruneBusy,
  onGenerate,
  onGenerateCodeWeaknesses,
  onApplyAll,
  onOpenReview,
  onPrune,
}: {
  actionPlan: ActionPlan;
  storeBusy: string | null;
  openSkillCount: number;
  openSkillManualReviewCount: number;
  canApplyAllOpenSkills: boolean;
  pruneBusy: boolean;
  onGenerate: () => void;
  onGenerateCodeWeaknesses: (scope: "incremental" | "full" | "deep") => void;
  onApplyAll: () => void;
  onOpenReview: () => void;
  onPrune: () => void;
}) {
  return (
    <Disclosure
      className="border-t border-line bg-surface-2 p-4 sm:p-5"
      summary={<span className="flex min-h-12 items-center text-sm font-semibold text-ink">Weitere Aktionen <span className="ml-1 text-xs font-normal text-ink-2">— Erzeugen · Scannen · Übernehmen · Aufräumen</span></span>}
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <OperatorActionCard
          icon={<Sparkles className="h-5 w-5" />}
          eyebrow="Schnell"
          hint={actionPlan.generate}
          title="Skill-Vorschläge holen"
          body="Sofort neue Kandidaten aus genutzten Skills erzeugen."
          button={<Button className="min-h-12 w-full justify-center" onClick={onGenerate} disabled={!!storeBusy} title={de.autoresearch.generateHint} prefix={storeBusy === "generate" ? <Spinner /> : <RotateCw className="h-4 w-4" />}>Vorschläge erzeugen</Button>}
        />
        <OperatorActionCard
          icon={<FlaskConical className="h-5 w-5" />}
          eyebrow="Code"
          hint={actionPlan.scan}
          title="Schwächen finden"
          body="Findet Code-Risiken und legt gegatete Vorschläge an."
          button={
            <div className="space-y-2">
              <Button outlined className="min-h-12 w-full justify-center" onClick={() => onGenerateCodeWeaknesses("incremental")} disabled={!!storeBusy} title={de.autoresearch.scanScopeHintChanged} prefix={storeBusy === codeWeaknessBusyKey("incremental") ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                Geänderte Dateien scannen
              </Button>
              <Disclosure className="rounded-panel border border-line bg-surface-2 p-2" summary={<span className="text-xs font-semibold text-ink">Mehr Umfang</span>}>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <Button outlined className="min-h-12 justify-center" onClick={() => onGenerateCodeWeaknesses("full")} disabled={!!storeBusy} title={de.autoresearch.scanScopeHintFull} prefix={storeBusy === codeWeaknessBusyKey("full") ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                    {de.autoresearch.scanScopeFull}
                  </Button>
                  <Button outlined className="min-h-12 justify-center" onClick={() => onGenerateCodeWeaknesses("deep")} disabled={!!storeBusy} title={de.autoresearch.deepScanHint} prefix={storeBusy === codeWeaknessBusyKey("deep") ? <Spinner /> : <SearchCode className="h-4 w-4" />}>
                    {de.autoresearch.scanScopeDeep}
                  </Button>
                </div>
              </Disclosure>
            </div>
          }
        />
        <OperatorActionCard
          icon={<ShieldCheck className="h-5 w-5" />}
          eyebrow="Review"
          hint={actionPlan.applySkills}
          title={openSkillManualReviewCount > 0 ? "Erst Review öffnen" : "Sichere Skills übernehmen"}
          body={openSkillManualReviewCount > 0 ? `${openSkillManualReviewCount} Skill-Vorschläge brauchen Einzelreview. Sammelübernahme bleibt gesperrt.` : "Nur batch-sichere Skill-Vorschläge gesammelt übernehmen; Code läuft einzeln durchs Gate."}
          button={openSkillManualReviewCount > 0 ? (
            <Button outlined className="min-h-12 w-full justify-center" onClick={onOpenReview} disabled={openSkillCount === 0} title="Öffnet die Entscheidungen, damit riskante Skill-Vorschläge einzeln geprüft werden." prefix={<ArrowDown className="h-4 w-4" />}>
              Review öffnen ({openSkillCount})
            </Button>
          ) : (
            <Button outlined className="min-h-12 w-full justify-center" onClick={onApplyAll} disabled={!canApplyAllOpenSkills} title={canApplyAllOpenSkills ? de.autoresearch.applyAllHint : "Keine batch-sicheren Skill-Vorschläge offen."} prefix={<GitPullRequestArrow className="h-4 w-4" />}>
              {de.autoresearch.applyAll} ({openSkillCount})
            </Button>
          )}
        />
        <OperatorActionCard
          icon={<Archive className="h-5 w-5" />}
          eyebrow="Pflege"
          hint={actionPlan.prune}
          title="Entscheidungen aufräumen"
          body="Archiviert Erledigtes und entfernt alte Kandidaten nach Backend-Regeln."
          button={<Button outlined className="min-h-12 w-full justify-center" onClick={onPrune} disabled={!!storeBusy || pruneBusy} title={de.autoresearch.pruneHint} prefix={pruneBusy ? <Spinner /> : <Archive className="h-4 w-4" />}>{de.autoresearch.prune}</Button>}
        />
      </div>
    </Disclosure>
  );
}
