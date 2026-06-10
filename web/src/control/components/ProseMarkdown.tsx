import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

// Phase C (Programm 3): Markdown-Renderer für LLM-/Web-Inhalte (Research-
// Antworten, Bibliotheks-Digests). Bewusst getrennt vom hausgemachten
// components/Markdown.tsx (No-Dep-Parser für eigene trusted Backlog-Specs):
// hier kommt fremder Modell-/Web-Text an, darum react-markdown + remark-gfm.
// Sicherheits-Vertrag (Planspec §7b): HTML bleibt AUS — KEIN rehype-raw,
// rohes HTML wird als Text gerendert, nie interpretiert. Links öffnen
// extern mit noopener. Lesetypografie via .hc-prose (control-tokens.css).
export function ProseMarkdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("hc-prose", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
