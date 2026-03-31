const CACHE = '__SW_CACHE_VERSION__';

self.addEventListener('install', (e) => {
  e.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const { request } = e;
  const url = new URL(request.url);

  // Never intercept API calls or auth redirects — always go to network
  if (url.pathname.startsWith('/v1/')) return;

  // Navigation (HTML): always network so index.html is always fresh
  // and auth redirects (?token=) always reach the server
  if (request.mode === 'navigate') return;

  // Static assets only: cache-first, lazily populate
  if (!url.pathname.startsWith('/assets/') && !url.pathname.startsWith('/icons/')) return;

  e.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE).then((c) => c.put(request, clone));
        }
        return response;
      });
    })
  );
});
