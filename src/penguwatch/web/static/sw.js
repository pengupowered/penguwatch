// PenguWatch service worker. Served from /sw.js with __ASSET_VERSION__ / __SHELL_URLS__ filled in.
// Caches only the versioned static shell; the API, the login page and anything that is not a GET
// are never cached and never touched.
const VERSION = "__ASSET_VERSION__";
const CACHE = "penguwatch-shell-" + VERSION;
const SHELL = __SHELL_URLS__;
const OFFLINE = "/static/v/" + VERSION + "/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      // every other cache of this origin is an older shell (including ones from before a rename)
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    if (url.pathname === "/login") return;
    // network first so a page is never stale; the offline page only appears when the network is gone
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
