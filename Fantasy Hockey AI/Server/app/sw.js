// The app's service worker: the shell's files, network first so an edit shows on the next load,
// the cached copy when the server can't be reached. The API's data is not cached yet (P6).
const CACHE = 'fh-shell-v1';
const SHELL = [
  './', 'index.html', 'app.css', 'app.js', 'api.js', 'manifest.webmanifest',
  'vendor/htm-preact-3.1.1.mjs', 'icons/icon-192.png',
  'fonts/ibm-plex-mono-latin-400-normal.woff2', 'fonts/ibm-plex-mono-latin-500-normal.woff2',
  'fonts/ibm-plex-mono-latin-600-normal.woff2',
  'fonts/ibm-plex-sans-condensed-latin-400-normal.woff2', 'fonts/ibm-plex-sans-condensed-latin-500-normal.woff2',
  'fonts/ibm-plex-sans-condensed-latin-600-normal.woff2', 'fonts/ibm-plex-sans-condensed-latin-700-normal.woff2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || !url.pathname.startsWith('/app/')) return;
  event.respondWith(fetch(event.request)
    .then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
      }
      return response;
    })
    .catch(() => caches.match(event.request).then((hit) => hit || caches.match('index.html'))));
});
