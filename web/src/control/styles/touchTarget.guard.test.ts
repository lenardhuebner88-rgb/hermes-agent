import { readFileSync, readdirSync } from "node:fs";
import { extname, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CONTROL_ROOT = resolve(import.meta.dirname, "..");
const TARGET_FLOOR_PX = 24;
const TARGET_CONTRACT =
  "No authored button/form/tab target may be smaller than 24×24 CSS px without a documented WCAG 2.5.8 exception.";

type SourceFile = { path: string; source: string };

function productionFiles(extension: ".tsx" | ".css"): SourceFile[] {
  const files: SourceFile[] = [];
  const walk = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      const controlPath = relative(CONTROL_ROOT, path);
      if (entry.isDirectory()) {
        if (entry.name !== "jarvis") walk(path);
      } else if (
        extname(entry.name) === extension &&
        !/\.(?:test|spec|stories)\./.test(entry.name) &&
        !(extension === ".css" && /(?:^|\/)jarvis[^/]*\.css$/.test(controlPath))
      ) {
        files.push({ path: controlPath, source: readFileSync(path, "utf8") });
      }
    }
  };
  walk(CONTROL_ROOT);
  return files.sort((a, b) => a.path.localeCompare(b.path));
}

function interactiveClasses(files: SourceFile[]): Set<string> {
  const classes = new Set<string>();
  const tagStartPattern = /<(?:button|a|select|input|textarea|summary)\b/g;
  const classNamePattern =
    /\bclassName\s*=\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`|\{([^}]*)\})/gis;
  const literalPattern = /"([^"]*)"|'([^']*)'|`([^`]*)`/gs;

  for (const { source } of files) {
    for (const tagStart of source.matchAll(tagStartPattern)) {
      let quote = "";
      let braces = 0;
      let end = tagStart.index;
      for (; end < source.length; end += 1) {
        const character = source[end];
        const previous = source[end - 1];
        if (quote) {
          if (character === quote && previous !== "\\") quote = "";
        } else if (character === '"' || character === "'" || character === "`") {
          quote = character;
        } else if (character === "{") {
          braces += 1;
        } else if (character === "}") {
          braces -= 1;
        } else if (character === ">" && braces === 0) {
          break;
        }
      }
      const tag = source.slice(tagStart.index, end + 1);
      for (const classNameMatch of tag.matchAll(classNamePattern)) {
        const directLiteral = classNameMatch[1] ?? classNameMatch[2] ?? classNameMatch[3];
        const literals = directLiteral === undefined
          ? [...(classNameMatch[4] ?? "").matchAll(literalPattern)].map(
              (match) => match[1] ?? match[2] ?? match[3],
            )
          : [directLiteral];
        for (const literal of literals) {
          for (const className of literal.split(/\s+/)) {
            if (/^-?[_a-zA-Z]+[_a-zA-Z0-9-]*$/.test(className)) classes.add(className);
          }
        }
      }
    }
  }
  return classes;
}

function declarations(body: string): Map<string, string> {
  return new Map(
    [...body.matchAll(/(?:^|;)\s*([a-z-]+)\s*:\s*([^;]+)/g)].map((match) => [
      match[1],
      match[2].trim(),
    ]),
  );
}

function verticalPaddingPx(value: string): number | null {
  const parts = value.trim().split(/\s+/);
  if (parts.length < 1 || parts.length > 4 || parts.some((part) => !/^[0-9.]+px$/.test(part))) {
    return null;
  }
  const px = parts.map(Number.parseFloat);
  return px.length <= 2 ? px[0] * 2 : px[0] + px[2];
}

function lineBoxPx(font: string): number | null {
  const match = font.match(/(?:^|\s)([0-9.]+)px\s*\/\s*([0-9.]+)(px)?(?:\s|$)/);
  if (!match) return null;
  const fontSize = Number.parseFloat(match[1]);
  const lineHeight = Number.parseFloat(match[2]);
  return match[3] ? lineHeight : fontSize * lineHeight;
}

function borderWidthPx(rules: Map<string, string>): number {
  const border = rules.get("border");
  const match = border?.match(/(?:^|\s)([0-9.]+)px(?:\s|$)/);
  return match ? Number.parseFloat(match[1]) : 0;
}

function targetViolations(cssFiles: SourceFile[], classNames: Set<string>): string[] {
  const violations: string[] = [];
  const rulePattern = /([^{}]+)\{([^{}]*)\}/g;
  const stateVariant = /:(?:hover|focus(?:-visible|-within)?|active|disabled|visited)\b/;

  for (const file of cssFiles) {
    for (const match of file.source.matchAll(rulePattern)) {
      const selector = match[1].trim().replace(/\/\*[\s\S]*?\*\//g, "").trim();
      if (
        !selector ||
        stateVariant.test(selector) ||
        ![...classNames].some((className) =>
          new RegExp(`\\.${className}(?![-_a-zA-Z0-9])`).test(selector),
        )
      ) {
        continue;
      }

      const rules = declarations(match[2]);
      let hasCompliantHeightFloor = false;
      for (const property of ["min-height", "height", "min-width", "width"]) {
        const value = rules.get(property);
        const px = value?.match(/^([0-9.]+)px$/);
        if (!px) continue;
        const size = Number.parseFloat(px[1]);
        if ((property === "min-height" || property === "height") && size >= TARGET_FLOOR_PX) {
          hasCompliantHeightFloor = true;
        }
        if (size < TARGET_FLOOR_PX) {
          violations.push(`${file.path} :: ${selector} :: ${property} ${size}px`);
        }
      }

      const padding = rules.get("padding");
      const font = rules.get("font");
      const verticalPadding = padding ? verticalPaddingPx(padding) : null;
      const lineBox = font ? lineBoxPx(font) : null;
      if (!hasCompliantHeightFloor && verticalPadding !== null && lineBox !== null) {
        const measuredHeight = verticalPadding + lineBox + 2 * borderWidthPx(rules);
        if (measuredHeight < TARGET_FLOOR_PX) {
          violations.push(
            `${file.path} :: ${selector} :: measured height ${measuredHeight.toFixed(1)}px`,
          );
        }
      }
    }
  }
  return violations;
}

describe("Control authored touch-target floor", () => {
  it("keeps statically measurable interactive targets at least 24×24 CSS px", () => {
    const classes = interactiveClasses(productionFiles(".tsx"));
    expect(
      ["fleet-copy-id", "fleet-copy", "fleet-boardtab-kref"].filter(
        (className) => !classes.has(className),
      ),
      "guard parser must discover the known regression controls",
    ).toEqual([]);
    const violations = targetViolations(productionFiles(".css"), classes);

    // Deliberate limits: rules without an explicit px size or their own padding+font
    // pair are not statically measurable (for example a multi-line
    // .st-ledger-worker-row). The Jarvis zone, non-semantic div/span click targets,
    // dead CSS classes absent from production TSX, and documented WCAG exceptions
    // are outside this guard's contract.
    expect(
      violations,
      `${violations.join("\n")}\n${TARGET_CONTRACT}`,
    ).toEqual([]);
  });
});
