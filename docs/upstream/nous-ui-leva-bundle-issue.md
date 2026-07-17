# Upstream issue draft: `@nous-research/ui` — Badge pulls leva + gsap into every consumer bundle

> Status: draft for filing against the `@nous-research/ui` repository (verified at v0.18.2).
> Submitted by the operator; this file is the source text.

---

**Title:** `Badge` component transitively bundles leva + gsap + nanostores (~250 KB chunk) via `BlendMode` → `use-smooth-controls`

## Summary

Importing the plain `Badge` component pulls the entire dev-tooling stack (leva, gsap, nanostores) into the consumer's production bundle, even though that code is only ever activated behind a `?dev` URL parameter.

## Import chain (verified in v0.18.2, `dist/`)

1. App imports `@nous-research/ui/ui/components/badge` (in our dashboard: 25 importing files).
2. `dist/ui/components/badge.js` imports `BlendMode` from `./blend-mode.js` (top-level, unconditional).
3. `dist/ui/components/blend-mode.js` imports `getControlAtom` from `../../hooks/use-smooth-controls.js` plus `@nanostores/react`.
4. `dist/hooks/use-smooth-controls.js` imports **`leva`** (`buttonGroup`, `useControls`), **`gsap`**, and **`nanostores`** at module top level.

Result in our Vite production build: a single `badge-*.js` chunk of **258,228 bytes (82.5 KB gzip)** that loads app-wide, because `Badge` is used in shared UI. Meanwhile the leva panel itself (`dist/ui/components/leva-client.js`) is explicitly gated at runtime:

```js
setHidden(!new URLSearchParams(window.location.search).has("dev"));
```

So ~99% of page loads pay ~82 KB gz for controls that are hidden unless the URL contains `?dev`.

## Why we can't easily work around it downstream

- `Badge` is the natural, documented import; forking or stubbing `blend-mode`/`use-smooth-controls` would have to replicate `useControls` + gsap tween semantics and would silently drift from upstream — a correctness risk we don't want to own.
- Vite/Rollup cannot tree-shake it: the leva/gsap imports are top-level and `useControls` runs on render, so the code is live as far as the bundler can prove.

## Suggested fixes (either works)

1. **Lazy-import the dev path:** make `blend-mode.js` load `use-smooth-controls` via dynamic `import()` only when the dev controls are actually enabled (the same `?dev` check that gates `LevaClient`), with a static default blend config otherwise. This splits leva/gsap into an async chunk that non-dev users never download.
2. **Make leva/gsap optional peers:** declare `leva` and `gsap` as `optionalPeerDependencies` and guard their usage, so consumers who don't install them get the static rendering path.

Option 1 is fully backwards-compatible for consumers and needs no packaging change.

## Environment

- `@nous-research/ui` 0.18.2, React 18, Vite 5/Rollup production build.
- Measured chunk: `badge-*.js` = 258,228 B raw / 82,490 B gzip, entry chunk of the app is 138,887 B raw / 45,721 B gzip for comparison — the badge chunk is ~1.8× our entire entry.

Happy to provide the full Rollup module graph or test a patched build.
