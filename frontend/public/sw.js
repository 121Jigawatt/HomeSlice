// Offline shell: the app UI loads without a connection; all data (/api) always goes to the server.
const CACHE = "homeslice-shell-v2";
const SHELL = ["/manifest.webmanifest", "/icon.svg", "/icon-192.png"];

// Stores index.html plus the hashed bundles it references, so the shell starts with no connection.
async function cacheShell(indexResponse) {
  const cache = await caches.open(CACHE);
  const html = await indexResponse.clone().text();
  const assets = [...new Set(html.match(/\/assets\/[^"'\s>]+/g) || [])];
  await Promise.all(assets.map((a) => cache.match(a).then((hit) => hit || cache.add(a))));
  // Drop bundles from older versions (lazy chunks are re-cached on next use).
  for (const req of await cache.keys()) {
    const path = new URL(req.url).pathname;
    if (path.startsWith("/assets/") && !assets.includes(path)) await cache.delete(req);
  }
  await cache.put("/", indexResponse);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => fetch("/", { cache: "no-cache" }))
      .then(cacheShell)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "refresh-shell") {
    event.waitUntil(fetch("/", { cache: "no-cache" }).then((res) => res.ok && cacheShell(res)).catch(() => undefined));
  }
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Never touch API calls, the OrcaSlicer desktop stream, or other origins.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/gui") || url.pathname === "/orca") return;

  if (req.mode === "navigate") {
    // While online let the browser navigate itself, so certificate warnings and server errors stay visible.
    if (self.navigator.onLine) return;
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) event.waitUntil(cacheShell(res.clone()).catch(() => undefined));
          return res;
        })
        .catch(() => caches.match("/")),
    );
    return;
  }

  if (url.pathname.startsWith("/assets/")) {
    // Hashed file names never change content: cache first.
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res.ok) caches.open(CACHE).then((c) => c.put(req, res.clone()));
            return res;
          }),
      ),
    );
  }
});
