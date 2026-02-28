const CACHE_NAME = 'cam-v46';

// Pre-cache key files on install so first load after SW activation is fast
const PRECACHE = [
  '/',
  '/js/app.js?v=46',
  '/js/api.js?v=46',
  '/js/state.js?v=46',
  '/js/views/dashboard.js?v=46',
  '/js/views/agent-detail.js?v=46',
  '/js/views/start-agent.js?v=46',
  '/js/views/contexts.js?v=46',
  '/js/views/settings.js?v=46',
  '/js/views/file-browser.js?v=46',
  '/css/style.css?v=38',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache =>
        // Use individual adds — don't let one failed file block SW installation
        Promise.all(PRECACHE.map(url =>
          cache.add(url).catch(() => {})
        ))
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const { request } = event;

  // API and WebSocket: network only
  if (request.url.includes('/api/') || request.url.includes('/ws')) {
    return;
  }

  // Static assets: cache-first, update in background
  // Versioned URLs (?v=XX) change when content changes, so cache is always valid
  event.respondWith(
    caches.match(request).then(cached => {
      // Background update: fetch fresh copy and update cache
      const fetchPromise = fetch(request).then(resp => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
        }
        return resp;
      }).catch(() => null);

      // Return cached immediately if available, otherwise wait for network
      return cached || fetchPromise;
    })
  );
});
