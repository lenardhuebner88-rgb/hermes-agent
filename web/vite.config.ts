import { defineConfig, type Plugin, type UserConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

const BACKEND = process.env.HERMES_DASHBOARD_URL ?? "http://127.0.0.1:9119";

type ViteConfigWithVitest = UserConfig & {
  test?: {
    exclude?: string[];
  };
};

/**
 * In production the Python `hermes dashboard` server injects a one-shot
 * session token into `index.html` (see `hermes_cli/web_server.py`). The
 * Vite dev server serves its own `index.html`, so unless we forward that
 * token, every protected `/api/*` call 401s.
 *
 * This plugin fetches the running dashboard's `index.html` on each dev page
 * load, scrapes the `window.__HERMES_SESSION_TOKEN__` assignment, and
 * re-injects it into the dev HTML. No-op in production builds.
 */
function hermesDevToken(): Plugin {
  const TOKEN_RE = /window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"/;
  const EMBEDDED_RE =
    /window\.__HERMES_DASHBOARD_EMBEDDED_CHAT__\s*=\s*(true|false)/;

  return {
    name: "hermes:dev-session-token",
    apply: "serve",
    async transformIndexHtml() {
      try {
        const res = await fetch(BACKEND, { headers: { accept: "text/html" } });
        const html = await res.text();
        const match = html.match(TOKEN_RE);
        if (!match) {
          console.warn(
            `[hermes] Could not find session token in ${BACKEND} — ` +
              `is \`hermes dashboard\` running? /api calls will 401.`,
          );
          return;
        }
        const embeddedMatch = html.match(EMBEDDED_RE);
        const embeddedJs = embeddedMatch ? embeddedMatch[1] : "true";
        return [
          {
            tag: "script",
            injectTo: "head",
            children:
              `window.__HERMES_SESSION_TOKEN__="${match[1]}";` +
              `window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=${embeddedJs};`,
          },
        ];
      } catch (err) {
        console.warn(
          `[hermes] Dashboard at ${BACKEND} unreachable — ` +
            `start it with \`hermes dashboard\` or set HERMES_DASHBOARD_URL. ` +
            `(${(err as Error).message})`,
        );
      }
    },
  };
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: "auto",
      manifest: false,
      workbox: {
        // `index.html` is rendered per-request by the Python server, which
        // injects auth bootstrap flags (`__HERMES_SESSION_TOKEN__` /
        // `__HERMES_AUTH_REQUIRED__`) that the SPA needs for its WebSocket
        // auth. The service worker must never answer navigations from the
        // precache instead — that would serve a static build HTML with
        // neither flag set, silently breaking WS auth (incident
        // 2026-07-03). Keep `html` out of the precache glob and disable the
        // navigate fallback outright.
        globPatterns: ["**/*.{js,css,svg,png,woff2}"],
        navigateFallback: null,
        importScripts: ["hermes-push-sw.js"],
        runtimeCaching: [],
      },
    }),
    hermesDevToken(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@hermes/shared": path.resolve(__dirname, "../apps/shared/src"),
    },
    // When @nous-research/ui is symlinked via `file:../../design-language`,
    // Node's module resolution would pick up shared deps from
    // design-language/node_modules/*, giving us two copies + breaking
    // hooks (useRef-of-null), webgl contexts, etc. Force everything that
    // exists in BOTH places to use the dashboard's copy.
    //
    // Don't list packages here that only exist in the DS (nanostores,
    // @nanostores/react) — Vite dedupe errors out when it can't find
    // them at the project root.
    dedupe: [
      "react",
      "react-dom",
      "@react-three/fiber",
      "@observablehq/plot",
      "three",
      "leva",
      "gsap",
    ],
  },
  build: {
    // Visual/self-verification builds must never dirty the tracked production
    // assets in a loop worktree. scripts/visual-verify.sh points this at a
    // disposable directory through the same HERMES_WEB_DIST variable the
    // Python dashboard server already understands.
    outDir: process.env.HERMES_WEB_DIST ?? "../hermes_cli/web_dist",
    emptyOutDir: true,
    // The eager entry chunk is ~785 kB minified (app bootstrap; React core is
    // already split into `vendor-react` below, heavy routes ChatPage/xterm and
    // ControlPage are already React.lazy code-split). That's a deliberate,
    // measured baseline — Vite's stock 500 kB warning threshold sits under it
    // and fired on every build as pure noise. A documented 900 kB ceiling still
    // flags a real regression (a deeper eager-bundle reduction lives on the
    // perf track, not here).
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // React core + router made up ~900 kB (pre-minify) of the 1 MB
        // eager index-*.js and change only on dependency bumps, yet every
        // app-code deploy rotated the index hash and re-downloaded the
        // whole thing (slow first paint on the phone). Splitting them into
        // their own chunk keeps that piece immutable-cached across deploys.
        // Deliberately ONLY this group: a blanket node_modules vendor chunk
        // would merge lazily-loaded deps (markdown/zod/framer-motion ride
        // in lazy route chunks) into the eager first load.
        manualChunks(id: string) {
          if (
            /node_modules\/(?:react|react-dom|scheduler|react-router|react-router-dom)\//.test(
              id,
            )
          ) {
            return "vendor-react";
          }
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: BACKEND,
        ws: true,
      },
      "/autoresearch": {
        target: BACKEND,
        ws: true,
      },
      // Same host as `hermes dashboard` must serve these; Vite has no
      // dashboard-plugins/* files, so without this, plugin scripts 404
      // or receive index.html in dev.
      "/dashboard-plugins": BACKEND,
    },
  },
  test: {
    exclude: ["node_modules/**", "dist/**", "e2e/**"],
    // Lastdeckel: ungedeckelt flutet vitest alle 12 Kerne und kollidiert mit
    // parallelen Worker-Gates/Builds (Load-Spike 2026-06-12). Wert wie im
    // Family Organizer nach derselben Lektion. minWorkers muss in vitest 2.x
    // explizit unter den Deckel, sonst RangeError gegen das CPU-Default-Minimum.
    minWorkers: 1,
    maxWorkers: 4,
    // Load-Flake-Deckel: unter maxWorkers=4-Contention reißen schwere
    // jsdom-Render-Tests den 5s-Default (2026-07-16: AgentTerminalsView,
    // danach Whack-a-Mole in weiteren Dateien). Suite-weit 15s statt pro Datei.
    testTimeout: 15000,
  },
} as ViteConfigWithVitest);
