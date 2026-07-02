import { isValidElement, useMemo, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { slugifyHeading } from "../lib/slug";

// Phase C (Programm 3): Markdown-Renderer für LLM-/Web-Inhalte (Research-
// Antworten, Bibliotheks-Digests). Bewusst getrennt vom hausgemachten
// components/Markdown.tsx (No-Dep-Parser für eigene trusted Backlog-Specs):
// hier kommt fremder Modell-/Web-Text an, darum react-markdown + remark-gfm.
// Sicherheits-Vertrag (Planspec §7b): HTML bleibt AUS — KEIN rehype-raw,
// rohes HTML wird als Text gerendert, nie interpretiert. Links öffnen
// extern mit noopener. Lesetypografie via .hc-prose (control-tokens.css).
//
// Interne Links (Wissens-Regal, Programm 3 S2): ein Aufrufer kann Markdown
// vorverarbeiten und `[Label](internal-link:<ziel>)` / `[Label](dead-link:<ziel>)`
// erzeugen (siehe knowledge.helpers `resolveWikiLinks`). Mit gesetztem
// `onInternalLink` navigieren solche Links intern statt extern zu öffnen;
// unauflösbare Ziele rendern als dezenter, nicht klickbarer Text. Ohne
// Vorverarbeitung tauchen diese Schemas in echtem Markdown nie auf → für alle
// anderen Aufrufer (Research, Bibliothek-Lesesaal) verhält sich `a` unverändert.

// Plain-Text aus gerenderten Heading-Children ziehen (für die id-Slugs).
function nodeText(children: ReactNode): string {
  if (children == null || typeof children === "boolean") return "";
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(nodeText).join("");
  if (isValidElement(children)) {
    return nodeText((children.props as { children?: ReactNode }).children);
  }
  return "";
}

/** Href-Schemas für vorverarbeitete interne Wiki-Links (siehe Datei-Kopf). */
export const PROSE_INTERNAL_LINK_SCHEME = "internal-link:";
export const PROSE_DEAD_LINK_SCHEME = "dead-link:";

// react-markdown saniert Hrefs standardmäßig auf http(s)/irc(s)/mailto/xmpp
// (`defaultUrlTransform`) und würde unsere beiden Schemas sonst stillschweigend
// zu "" leeren. Für alles andere bleibt exakt das Standardverhalten (Sicherheits-
// Vertrag unverändert) — nur die zwei selbst erzeugten Schemas passieren roh durch.
function urlTransform(value: string): string {
  if (value.startsWith(PROSE_INTERNAL_LINK_SCHEME) || value.startsWith(PROSE_DEAD_LINK_SCHEME)) {
    return value;
  }
  return defaultUrlTransform(value);
}

/** Baut die `a`-Komponente: erkennt die beiden `PROSE_*_LINK_SCHEME`-Präfixe
 *  (interner Link / toter Link), sonst unverändert extern mit noopener. */
function buildLinkComponent(onInternalLink?: (target: string) => void): Components["a"] {
  return ({ href, children, ...rest }) => {
    if (typeof href === "string" && href.startsWith(PROSE_DEAD_LINK_SCHEME)) {
      const target = decodeURIComponent(href.slice(PROSE_DEAD_LINK_SCHEME.length));
      return (
        <span
          className="cursor-not-allowed underline decoration-dotted underline-offset-2 hc-dim"
          title={`Ziel nicht gefunden: ${target}`}
        >
          {children}
        </span>
      );
    }
    if (typeof href === "string" && href.startsWith(PROSE_INTERNAL_LINK_SCHEME)) {
      const target = decodeURIComponent(href.slice(PROSE_INTERNAL_LINK_SCHEME.length));
      return (
        <a
          {...rest}
          href="#"
          onClick={(e) => {
            e.preventDefault();
            onInternalLink?.(target);
          }}
        >
          {children}
        </a>
      );
    }
    return (
      <a {...rest} href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  };
}

// Heading-Komponenten, die eine id aus dem Slug vergeben (für klickbare TOC-
// Anker im Nachschlagewerk). slugifyHeading ist dieselbe Funktion wie in
// extractToc → die Anker stimmen garantiert. scroll-mt: beim Anspringen nicht
// unter einem Sticky-Header verschwinden.
const H1: Components["h1"] = ({ children, ...rest }) => (
  <h1 id={slugifyHeading(nodeText(children)) || "abschnitt"} className="scroll-mt-20" {...rest}>{children}</h1>
);
const H2: Components["h2"] = ({ children, ...rest }) => (
  <h2 id={slugifyHeading(nodeText(children)) || "abschnitt"} className="scroll-mt-20" {...rest}>{children}</h2>
);
const H3: Components["h3"] = ({ children, ...rest }) => (
  <h3 id={slugifyHeading(nodeText(children)) || "abschnitt"} className="scroll-mt-20" {...rest}>{children}</h3>
);

// Breite GFM-Tabellen (z. B. das Modell-Landschaft-Regal) horizontal
// scrollbar machen statt sie auf Mobile zu quetschen — .hc-prose table hat
// width:100%, min-width erzwingt hier das Überlaufen im Wrapper statt im
// Artikel-Layout. Nur aktiv, wenn `wrapTables` gesetzt ist (additiv).
const TABLE_WRAP: Components["table"] = ({ children, ...rest }) => (
  <div className="overflow-x-auto">
    <table {...rest} style={{ minWidth: "32rem" }}>{children}</table>
  </div>
);

export function ProseMarkdown({
  children,
  className,
  slugHeadings = false,
  wrapTables = false,
  onInternalLink,
}: {
  children: string;
  className?: string;
  /** Vergibt h1–h3 eine Slug-`id` für klickbare Inhaltsverzeichnisse. */
  slugHeadings?: boolean;
  /** Wrapt `<table>` in einen `overflow-x-auto`-Container (Mobile-Scroll). */
  wrapTables?: boolean;
  /** Siehe Datei-Kopf: aktiviert interne Navigation für vorverarbeitete
   *  `internal-link:`/`dead-link:`-Hrefs. Ohne Callback bleiben solche Hrefs
   *  (die außerhalb vorverarbeiteten Markdowns nie vorkommen) folgenlos. */
  onInternalLink?: (target: string) => void;
}) {
  const components = useMemo<Components>(() => ({
    a: buildLinkComponent(onInternalLink),
    ...(slugHeadings ? { h1: H1, h2: H2, h3: H3 } : {}),
    ...(wrapTables ? { table: TABLE_WRAP } : {}),
  }), [onInternalLink, slugHeadings, wrapTables]);

  return (
    <div className={cn("hc-prose", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={urlTransform}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
