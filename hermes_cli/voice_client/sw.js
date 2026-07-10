"use strict";

const CACHE = "hermes-voice-v1";
const CORE_ASSETS = [
  "/voice/offline.html",
  "/voice/app.js",
  "/voice/worklet.js",
  "/voice/manifest.json",
  "/voice/icon.svg",
  "/voice/icon-192.png",
  "/voice/icon-512.png",
  "/voice/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((name) => name !== CACHE).map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/")) {
    // Never intercept API calls: they carry live session state that must
    // never be served stale or from a cache keyed on the wrong credentials.
    return;
  }

  if (request.mode === "navigate") {
    // Network-first for the document itself. The response can embed a
    // per-session bootstrap token (see _voice_index_response), so it must
    // never be cached — only the static offline fallback is cacheable.
    event.respondWith(
      fetch(request).catch(() => caches.match("/voice/offline.html")),
    );
    return;
  }

  if (url.origin !== self.location.origin || !url.pathname.startsWith("/voice/")) {
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.status === 200 && response.type === "basic") {
          const copy = response.clone();
          // waitUntil keeps the worker alive until the write lands —
          // otherwise the browser may terminate it mid-put and offline
          // loads would keep serving the previous asset version.
          event.waitUntil(caches.open(CACHE).then((cache) => cache.put(request, copy)));
        }
        return response;
      })
      .catch(() => caches.match(request)),
  );
});
