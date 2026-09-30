// Jarvis is no longer an installable web app (the Android app replaced it). Browsers that
// installed the old version still run its service worker; this replacement deletes its
// cached copy of the page and unregisters itself, so the page always comes from Jarvis.
// Safe to delete this file once no browser has the old app installed (e.g. 2027).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.registration.unregister();
    for (const client of await self.clients.matchAll({ type: "window" })) client.navigate(client.url);
  })());
});
