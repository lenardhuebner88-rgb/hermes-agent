import type { ActivityEntry, ToneName } from "./types";

export interface AutoresearchActivityCard {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
}

export function getAutoresearchActivityCard(entry: ActivityEntry): AutoresearchActivityCard {
  if (entry.tone === "red") {
    return {
      tone: "red",
      label: "Fehler",
      title: "Diese Aktion braucht Prüfung.",
      detail: entry.text,
      next: "Fehlertext lesen; danach erst dieselbe Aktion erneut auslösen.",
    };
  }
  if (entry.tone === "amber") {
    return {
      tone: "amber",
      label: "Achtung",
      title: "Diese Aktion war nicht neutral.",
      detail: entry.text,
      next: "Kurz prüfen, ob dadurch Entscheidungen, Lauf oder Auswahl anders stehen.",
    };
  }
  if (entry.tone === "emerald") {
    return {
      tone: "emerald",
      label: "Erledigt",
      title: "Die Aktion ist abgeschlossen.",
      detail: entry.text,
      next: "Weiter mit dem nächsten sicheren Schritt im Cockpit.",
    };
  }
  return {
    tone: "cyan",
    label: "Info",
    title: "Statusmeldung aus Autoresearch.",
    detail: entry.text,
    next: "Als Kontext lesen; keine direkte Aktion nötig.",
  };
}
