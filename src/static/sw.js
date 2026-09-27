// Makes Jarvis an installable app, and lets it open while Jarvis is off: then ngrok
// answers "ERR_NGROK_3200 offline", and we show our own page (with a Start button)
// instead. Only the page itself and its icons are cached: never /api/ data, never
// conversations.
const CACHE = "jarvis-shell-v1";
const SHELL = ["./", "manifest.json", "icon-192.png", "icon-512.png", "apple-touch-icon.png"];
const HEADERS = { "ngrok-skip-browser-warning": "1" };

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    for (const url of SHELL) {  // one by one: a single failure must not block install
      try {
        const res = await fetch(url, { headers: HEADERS, credentials: "include" });
        if (res.ok && !res.headers.get("ngrok-error-code")) await cache.put(url, res);
      } catch (e) {}
    }
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

function isOfflineAnswer(res) {
  return !res.ok && (res.headers.get("ngrok-error-code") || res.status >= 500 || res.status === 404);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.includes("/api/")) {
    return;  // live data always goes straight to the network
  }
  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      // Google sign-in redirects must pass through untouched.
      if (res.type === "opaqueredirect" || res.redirected) return res;
      if (isOfflineAnswer(res)) {
        const cached = await caches.match(req.mode === "navigate" ? "./" : req, { ignoreSearch: true });
        if (cached) return cached;
      } else if (res.ok && SHELL.some((p) => url.pathname.endsWith(p.replace("./", "/")))) {
        const cache = await caches.open(CACHE);
        cache.put(req.mode === "navigate" ? "./" : req, res.clone());  // keep the copy fresh
      }
      return res;
    } catch (e) {  // no network at all
      const cached = await caches.match(req.mode === "navigate" ? "./" : req, { ignoreSearch: true });
      return cached || Response.error();
    }
  })());
});
