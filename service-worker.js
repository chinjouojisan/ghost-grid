// Ghost Grid — service worker
//
// Two strategies, split by file:
//
// 1. App shell (this file's logic, HTML structure, icons, manifest) —
//    cache-first, so the app opens instantly and works offline once
//    installed. CACHE_VERSION only needs bumping when THIS kind of file
//    changes (new features, layout/logic changes).
//
// 2. Data (./data.json — draw numbers, prediction log) — network-first,
//    cache fallback. Online, every load fetches the freshest data.json from
//    the server and updates the cache in the background. Offline, it falls
//    back to whatever was last cached. This means data updates (new draws)
//    no longer require bumping CACHE_VERSION at all — just replace data.json
//    on the server, and the next online launch picks it up automatically.
const CACHE_VERSION = 'ghost-grid-v8';
const DATA_FILE = './data.json';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-180.png',
  './favicon-32.png'
];

self.addEventListener('install', (event) => {
  // NOT cache.addAll(): addAll is all-or-nothing, so one missing file (a
  // 404'd icon, a typo'd filename) silently fails the ENTIRE install and the
  // service worker never activates — offline breaks completely over one
  // asset. Each file is added independently instead, so a single miss just
  // means that one file isn't cached; everything else still works offline.
  // data.json is included here too (best-effort) so the very first offline
  // visit after install still has a copy to fall back to.
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      Promise.all(
        [...APP_SHELL, DATA_FILE].map((url) =>
          cache.add(url).catch((err) => console.warn('[SW] cache miss, skipping:', url, err))
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isDataRequest(request) {
  return new URL(request.url).pathname.endsWith('/data.json');
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  if (isDataRequest(event.request)) {
    // network-first: always try the live server copy first, since this is
    // exactly the data that's supposed to update without a version bump.
    // Only fall back to whatever's cached if the network is unreachable.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // everything else (app shell): cache-first, refresh in the background
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached); // offline: fall back to whatever is cached
      return cached || network;
    })
  );
});
