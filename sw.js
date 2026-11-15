// Network-first for everything; fall back to cache when offline.
const CACHE = "moto-parking-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.hostname.endsWith("tile.openstreetmap.org")) return;
  e.respondWith(
    fetch(e.request, url.origin === location.origin ? { cache: "no-cache" } : {})
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request)),
  );
});
