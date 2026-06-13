import { isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
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

const LINK: Components["a"] = (props) => <a {...props} target="_blank" rel="noopener noreferrer" />;

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

const SLUG_COMPONENTS: Components = { a: LINK, h1: H1, h2: H2, h3: H3 };

const PLAIN_COMPONENTS: Components = { a: LINK };

export function ProseMarkdown({
  children,
  className,
  slugHeadings = false,
}: {
  children: string;
  className?: string;
  /** Vergibt h1–h3 eine Slug-`id` für klickbare Inhaltsverzeichnisse. */
  slugHeadings?: boolean;
}) {
  return (
    <div className={cn("hc-prose", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={slugHeadings ? SLUG_COMPONENTS : PLAIN_COMPONENTS}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
