// No-dep Markdown renderer for trusted internal .md files (orchestration backlog specs).
// Covers: headings h1–h6, ul/ol, fenced + inline code, bold/italic, links, blockquote,
// HR, simple pipe tables, and paragraphs. Source files are own orchestration specs — no
// untrusted input, no sanitisation needed.
import { cn } from "@/lib/utils";

type Block =
  | { type: "h"; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] }
  | { type: "code"; lang: string; text: string }
  | { type: "blockquote"; lines: string[] }
  | { type: "hr" }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "p"; text: string };

function parseMd(markdown: string): Block[] {
  const lines = markdown.split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) { i++; continue; }

    // Fenced code block
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // consume closing ```
      blocks.push({ type: "code", lang, text: codeLines.join("\n") });
      continue;
    }

    // Heading
    const hm = /^(#{1,6})\s+(.+)$/.exec(trimmed);
    if (hm) {
      blocks.push({ type: "h", level: hm[1].length as 1, text: hm[2] });
      i++;
      continue;
    }

    // HR
    if (/^[-*_]{3,}$/.test(trimmed)) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    // Blockquote
    if (trimmed.startsWith(">")) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      blocks.push({ type: "blockquote", lines: quoteLines });
      continue;
    }

    // Table (line starts and ends with |)
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i]);
        i++;
      }
      if (tableLines.length >= 2) {
        const parseRow = (row: string) =>
          row.split("|").slice(1, -1).map((c) => c.trim());
        const headers = parseRow(tableLines[0]);
        const rows = tableLines.slice(2).map(parseRow); // skip separator
        if (headers.length > 0) { blocks.push({ type: "table", headers, rows }); continue; }
      }
      continue;
    }

    // Unordered list
    if (/^\s*[-*+]\s/.test(trimmed)) {
      const listItems: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s/.test(lines[i].trim())) {
        listItems.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i++;
      }
      blocks.push({ type: "ul", items: listItems });
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s/.test(trimmed)) {
      const listItems: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s/.test(lines[i].trim())) {
        listItems.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ type: "ol", items: listItems });
      continue;
    }

    // Paragraph: collect until blank line or block-level start
    const pLines: string[] = [];
    while (i < lines.length) {
      const l = lines[i].trim();
      if (!l) break;
      if (/^#{1,6}\s|^\s*[-*+]\s|^\s*\d+\.\s|^>|^```|^[-*_]{3,}$/.test(l)) break;
      if (l.startsWith("|") && l.endsWith("|")) break;
      pLines.push(lines[i]);
      i++;
    }
    if (pLines.length) blocks.push({ type: "p", text: pLines.join(" ").trim() });
  }

  return blocks;
}

// Inline: links, `code`, **bold**, *italic*, __bold__, _italic_
const INLINE_RE = /\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|__([^_]+)__|_([^_]+)_/g;

function formatInline(text: string, keyPrefix = ""): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${m.index}`;
    if (m[1] && m[2]) {
      const href = m[2].trim();
      if (!/^(https?:|mailto:|#)/i.test(href)) {
        parts.push(m[1]);
      } else {
        parts.push(<a key={key} href={href} className="text-live underline underline-offset-2" target="_blank" rel="noreferrer">{m[1]}</a>);
      }
    } else if (m[3]) {
      parts.push(<code key={key} className="rounded bg-surface-2 px-1 font-mono text-xs text-ink">{m[3]}</code>);
    } else if (m[4] ?? m[6]) {
      parts.push(<strong key={key} className="font-semibold text-ink">{m[4] ?? m[6]}</strong>);
    } else if (m[5] ?? m[7]) {
      parts.push(<em key={key} className="italic text-ink-2">{m[5] ?? m[7]}</em>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

const H_CLASS: Record<number, string> = {
  1: "mt-5 mb-2 text-base font-bold text-ink",
  2: "mt-4 mb-2 text-sm font-semibold text-ink",
  3: "mt-3 mb-1 text-sm font-medium text-ink",
  4: "mt-3 mb-1 text-xs font-medium text-ink-2",
  5: "mt-2 mb-1 text-xs text-ink-3",
  6: "mt-2 mb-1 text-xs text-ink-3",
};

export function Markdown({ body, className }: { body: string; className?: string }) {
  if (!body.trim()) {
    return <p className="text-xs italic hc-dim">Keine Beschreibung.</p>;
  }
  const blocks = parseMd(body);
  return (
    <div className={cn("space-y-2 text-sm text-ink-2", className)}>
      {blocks.map((block, idx) => {
        switch (block.type) {
          case "hr":
            return <hr key={idx} className="border-line-soft" />;
          case "h": {
            const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
            return <Tag key={idx} className={H_CLASS[block.level]}>{formatInline(block.text, String(idx))}</Tag>;
          }
          case "p":
            return <p key={idx} className="leading-relaxed">{formatInline(block.text, String(idx))}</p>;
          case "code":
            return (
              <pre key={idx} className="overflow-x-auto rounded-lg bg-surface-0 px-3 py-2 text-xs leading-5 text-ink hc-mono">
                <code>{block.text}</code>
              </pre>
            );
          case "blockquote":
            return (
              <blockquote key={idx} className="border-l-2 border-line pl-3 italic text-ink-2">
                {block.lines.map((l, j) => <span key={j} className="block">{formatInline(l, `${idx}-${j}`)}</span>)}
              </blockquote>
            );
          case "ul":
            return (
              <ul key={idx} className="list-disc space-y-0.5 pl-5">
                {block.items.map((item, j) => <li key={j}>{formatInline(item, `${idx}-${j}`)}</li>)}
              </ul>
            );
          case "ol":
            return (
              <ol key={idx} className="list-decimal space-y-0.5 pl-5">
                {block.items.map((item, j) => <li key={j}>{formatInline(item, `${idx}-${j}`)}</li>)}
              </ol>
            );
          case "table":
            return (
              <div key={idx} className="overflow-x-auto rounded-lg border border-line">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-line bg-surface-2">
                      {block.headers.map((h, j) => (
                        <th key={j} className="px-3 py-2 text-left font-semibold text-ink">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, j) => (
                      <tr key={j} className={cn("border-b border-line-soft", j % 2 === 1 && "bg-surface-2/50")}>
                        {row.map((cell, k) => (
                          <td key={k} className="px-3 py-1.5 text-ink-2">{formatInline(cell, `${idx}-${j}-${k}`)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
