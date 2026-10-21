// The app's service worker. The shell's files: network first so an edit shows on the next load,
// the cached copy when the server can't be reached. The screens' data (SAVED_API): network first
// too, each answer kept; with the server out of reach the last one is served, marked with
// X-Saved-At (when it was fetched) so the app can say it is stale. Live game calls are never kept:
// an old score is worse than none.
const SHELL_CACHE = 'fh-shell-v49';
const DATA_CACHE = 'fh-data-v1';
const LOGO_CACHE = 'fh-logos-v1';   // the NHL's team logos (assets.nhle.com): kept, they rarely change
const LOGOS = 'https://assets.nhle.com/logos/';
const MUG_CACHE = 'fh-mugs-v1';    // players' headshots (assets.nhle.com/mugs): kept, one a season and team
const MUGS = 'https://assets.nhle.com/mugs/';
const SHELL = [
  './', 'index.html', 'app.css', 'app.js', 'api.js', 'layout.js', 'roster.js', 'ai.js', 'planner.js', 'games.js', 'matchup.js',
  'manifest.webmanifest', 'vendor/htm-preact-3.1.1.mjs', 'icons/icon-192.png', 'icons/icon-512.png',
  'fonts/ibm-plex-mono-latin-400-normal.woff2', 'fonts/ibm-plex-mono-latin-500-normal.woff2',
  'fonts/ibm-plex-mono-latin-600-normal.woff2',
  'fonts/ibm-plex-sans-condensed-latin-400-normal.woff2', 'fonts/ibm-plex-sans-condensed-latin-500-normal.woff2',
  'fonts/ibm-plex-sans-condensed-latin-600-normal.woff2', 'fonts/ibm-plex-sans-condensed-latin-700-normal.woff2',
];
const SAVED_API = ['/status', '/plan', '/roster', '/suggestions', '/week', '/matchup', '/choices'];
const SAVED_EXACT = ['/jobs'];      // the refresh log; a running job's own lines are never kept

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL.map((u) => new Request(u, { cache: 'no-cache' })))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => ![SHELL_CACHE, DATA_CACHE, LOGO_CACHE, MUG_CACHE].includes(k)).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// The shell is always checked with the server (`no-cache`: a quick 304 when unchanged): Cloudflare
// rewrites the server's no-cache to a 4-hour browser lifetime, which kept an old app.css for hours.
async function shell(request) {
  try {
    const response = await fetch(request, { cache: 'no-cache' });
    if (response.ok) {
      const copy = response.clone();
      caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch (error) {
    return (await caches.match(request)) || (await caches.match('index.html')) || Response.error();
  }
}

async function data(request) {
  const cache = await caches.open(DATA_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      const body = await response.clone().arrayBuffer();
      const headers = new Headers(response.headers);
      headers.set('X-Saved-At', new Date().toISOString());
      cache.put(request, new Response(body, { status: 200, headers }));
    }
    return response;
  } catch (error) {
    const saved = await cache.match(request);
    if (saved) return saved;
    throw error;
  }
}

// A logo or headshot from the cache when it has one, else fetched and kept.
async function kept(request, name) {
  const cache = await caches.open(name);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok || response.type === 'opaque') cache.put(request, response.clone());
  return response;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'GET' && event.request.url.startsWith(LOGOS)) {
    event.respondWith(kept(event.request, LOGO_CACHE));
    return;
  }
  if (event.request.method === 'GET' && event.request.url.startsWith(MUGS)) {
    event.respondWith(kept(event.request, MUG_CACHE));
    return;
  }
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/app/')) {
    event.respondWith(shell(event.request));
  } else if (SAVED_EXACT.includes(url.pathname)
             || SAVED_API.some((p) => url.pathname === p || url.pathname.startsWith(`${p}/`))) {
    event.respondWith(data(event.request));
  }
});
