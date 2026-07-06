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

/**
 * True when wheel input should be handed to xterm's own report/arrow-key
 * logic instead of being consumed by the local scrollLines() fallback.
 *
 * xterm's alternate buffer (what fullscreen TUIs like tmux/vim run in) has
 * no scrollback of its own — `scrollLines()` on it is a structural no-op.
 * Deferring lets xterm run its internal wheel handling (SGR mouse reports
 * when the app has mouse tracking on, or its wheel→arrow-key fallback
 * otherwise), which is the only path that can scroll such an app at all.
 */
export function shouldDeferWheelToTerminal(bufferType: "normal" | "alternate"): boolean {
  return bufferType === "alternate";
}

export interface HermesXtermSurfaceOptions {
  host: HTMLElement;
  theme: ITheme;
  scrollback?: number;
  loggerName: string;
  onWheelScrollBuffer?: boolean;
  /**
   * Opt-in: defer wheel events to xterm's own handling while the alternate
   * buffer is active (see `shouldDeferWheelToTerminal`). Off by default so
   * ChatPage's mouse-disabled TUI keeps its existing local-scroll behavior
   * byte-identical.
   */
  appAwareWheel?: boolean;
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
  appAwareWheel = false,
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
      if (appAwareWheel && shouldDeferWheelToTerminal(term.buffer.active.type)) {
        // Let xterm run its own wheel handling (SGR mouse report or its
        // built-in wheel→arrow fallback) — returning true here means we did
        // NOT suppress it, unlike the `return false` path below.
        return true;
      }
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

// ----- touch-drag scroll bridge (alternate-buffer TUIs only) ----------------
// SGR mouse wheel reports (`CSI < Cb ; Cx ; Cy M`) — button code 64 is wheel
// UP (scroll back in history), 65 is wheel DOWN (tmux: MOUSE_WHEEL_UP == 64
// in tmux.h). Column/row (1;1) are irrelevant for wheel scroll bindings, tmux
// only inspects the button code, but a valid coordinate pair is required by
// the SGR encoding.
export const SGR_WHEEL_UP = "\x1b[<64;1;1M";
export const SGR_WHEEL_DOWN = "\x1b[<65;1;1M";

/**
 * Turns an accumulated touch-drag delta (px) into whole scroll "steps" plus
 * the leftover remainder to carry into the next call. Pure so the touch
 * gesture math is unit-testable without a DOM/xterm instance.
 *
 * Caller convention: `accumulatedPx` is the previous call's `remainder` plus
 * the newest per-move delta — each call consumes as many whole `stepPx`
 * increments as fit and hands back what's left. Sign is preserved (negative
 * `accumulatedPx` yields negative `steps`), so callers can tell scroll
 * direction from the sign of `steps`.
 */
export function touchScrollSteps(accumulatedPx: number, stepPx: number): { steps: number; remainder: number } {
  if (stepPx <= 0) return { steps: 0, remainder: accumulatedPx };
  // `|| 0` normalizes -0 (e.g. Math.trunc(-15 / 20)) to plain 0 — the sign
  // only matters once at least one whole step has accumulated.
  const steps = Math.trunc(accumulatedPx / stepPx) || 0;
  const remainder = accumulatedPx - steps * stepPx;
  return { steps, remainder };
}
