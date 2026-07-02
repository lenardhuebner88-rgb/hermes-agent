import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal, type ITerminalOptions, type ITheme } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

export const TERMINAL_THEME_STATIC: ITheme = {
  foreground: "#f0e6d2",
  cursor: "#f0e6d2",
  cursorAccent: "#0d2626",
  selectionBackground: "#f0e6d244",
};

export function terminalTierWidthPx(host: HTMLElement | null): number {
  if (typeof window === "undefined") return 1280;
  const fromHost = host?.clientWidth ?? 0;
  if (fromHost > 2) return Math.round(fromHost);
  const doc = document.documentElement?.clientWidth ?? 0;
  const vv = window.visualViewport;
  const inner = window.innerWidth;
  const vvw = vv?.width ?? inner;
  const layout = Math.min(inner, vvw, doc > 0 ? doc : inner);
  return Math.max(1, Math.round(layout));
}

export function terminalFontSizeForWidth(layoutWidthPx: number): number {
  if (layoutWidthPx < 300) return 8;
  if (layoutWidthPx < 420) return 10;
  if (layoutWidthPx < 520) return 11;
  if (layoutWidthPx < 720) return 12;
  if (layoutWidthPx < 1024) return 13;
  return 14;
}

export function terminalLineHeightForWidth(layoutWidthPx: number): number {
  return layoutWidthPx < 1024 ? 1.2 : 1.15;
}

export function hardenTerminalTextInput(host: HTMLElement): void {
  const apply = () => {
    const textareas = host.querySelectorAll<HTMLTextAreaElement>("textarea.xterm-helper-textarea, textarea");
    textareas.forEach((textarea) => {
      textarea.setAttribute("autocomplete", "off");
      textarea.setAttribute("autocorrect", "off");
      textarea.setAttribute("autocapitalize", "off");
      textarea.setAttribute("spellcheck", "false");
      textarea.setAttribute("aria-autocomplete", "none");
      textarea.setAttribute("enterkeyhint", "send");
      textarea.setAttribute("data-ms-editor", "false");
      textarea.spellcheck = false;
    });
  };

  apply();
  requestAnimationFrame(apply);
}

export interface HermesXtermSurfaceOptions {
  host: HTMLElement;
  theme: ITheme;
  scrollback?: number;
  loggerName: string;
  onWheelScrollBuffer?: boolean;
  terminalOptions?: Partial<ITerminalOptions>;
}

export interface HermesXtermSurface {
  term: Terminal;
  fit: FitAddon;
}

export function createHermesXtermSurface({
  host,
  theme,
  scrollback = 5000,
  loggerName,
  onWheelScrollBuffer = true,
  terminalOptions = {},
}: HermesXtermSurfaceOptions): HermesXtermSurface {
  const tierW0 = terminalTierWidthPx(host);
  const term = new Terminal({
    allowProposedApi: true,
    cursorBlink: true,
    fontFamily:
      "'JetBrains Mono', 'Cascadia Mono', 'Fira Code', 'MesloLGS NF', 'Source Code Pro', Menlo, Consolas, 'DejaVu Sans Mono', monospace",
    fontSize: terminalFontSizeForWidth(tierW0),
    lineHeight: terminalLineHeightForWidth(tierW0),
    letterSpacing: 0,
    fontWeight: "400",
    fontWeightBold: "700",
    macOptionIsMeta: true,
    macOptionClickForcesSelection: true,
    rightClickSelectsWord: true,
    // Agent TUIs (codex, kimi-code, claude) style secondary text with dark
    // 256-color grays (38;5;240/241/244) and SGR dim — invisible against the
    // dark terminal background because the theme defines no ANSI palette.
    // 4.5 = WCAG AA; xterm lightens any foreground (16/256/truecolor, dim at
    // half ratio by design) that falls below it. Powerline glyphs exempt.
    minimumContrastRatio: 4.5,
    scrollback,
    theme,
    ...terminalOptions,
  });

  const fit = new FitAddon();
  term.loadAddon(fit);

  if (onWheelScrollBuffer) {
    term.attachCustomWheelEventHandler((ev) => {
      const delta = ev.deltaY;
      if (!delta) return false;
      const step = Math.max(1, Math.round(Math.abs(delta) / 50));
      term.scrollLines(delta > 0 ? step : -step);
      ev.preventDefault();
      ev.stopPropagation();
      return false;
    });
  }

  const unicode11 = new Unicode11Addon();
  term.loadAddon(unicode11);
  term.unicode.activeVersion = "11";
  term.loadAddon(new WebLinksAddon());
  term.open(host);
  hardenTerminalTextInput(host);

  if (terminalTierWidthPx(host) >= 768) {
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => webgl.dispose());
      term.loadAddon(webgl);
    } catch (err) {
      console.warn(`[${loggerName}] WebGL renderer unavailable; falling back to default`, err);
    }
  }

  return { term, fit };
}
