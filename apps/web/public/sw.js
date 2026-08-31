const CACHE_NAME = 'smart-signals-static-v6';
const APP_SHELL = '/';
const STATIC_ASSETS = [APP_SHELL, '/manifest.webmanifest', '/smart-signals-app-icon.png', '/super-signals-logo.png'];
const PRIVATE_PREFIXES = ['/api/', '/auth/', '/account/', '/admin/', '/owner/', '/notifications'];
const CACHEABLE_DESTINATIONS = new Set(['image', 'font']);

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

async function networkFirstWithFallback(request, fallbackPath = null) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: 'no-store' });
    if (response.ok) {
      await cache.put(request, response.clone());
      return response;
    }
    if (response.status < 500) return response;
  } catch {
    // A Render restart can temporarily remove the only origin instance. Fall through
    // to the last healthy app shell/assets rather than exposing Render's 502 page.
  }

  const cached = await cache.match(request);
  if (cached) return cached;
  if (fallbackPath) {
    const fallback = await cache.match(fallbackPath);
    if (fallback) return fallback;
  }
  return new Response('Smart Signals is reconnecting. Please try again shortly.', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (PRIVATE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;

  // Keep authenticated API/account reads strictly live and uncached, but make the
  // application shell resilient to the brief zero-instance window Render can create
  // during a restart. Successful HTML/JS/CSS is refreshed immediately and retained
  // only as a fallback for the next transport outage.
  if (request.mode === 'navigate') {
    event.respondWith(networkFirstWithFallback(request, APP_SHELL));
    return;
  }

  if (request.destination === 'script' || request.destination === 'style') {
    event.respondWith(networkFirstWithFallback(request));
    return;
  }

  const isKnownStaticAsset = STATIC_ASSETS.includes(url.pathname);
  if (!isKnownStaticAsset && !CACHEABLE_DESTINATIONS.has(request.destination)) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (!response.ok) return response;
        const copy = response.clone();
        void caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      });
    }),
  );
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }

  const notificationId = typeof payload.notification_id === 'string' ? payload.notification_id : null;
  const title =
    typeof payload.title === 'string' && payload.title.trim() ? payload.title.trim() : 'Smart Signals';
  const body = typeof payload.body === 'string' ? payload.body : 'Trade update available.';
  const rawUrl = typeof payload.url === 'string' ? payload.url : '/';
  const safeUrl = rawUrl.startsWith('/') ? rawUrl : '/';

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/smart-signals-app-icon.png',
      badge: '/smart-signals-app-icon.png',
      tag: notificationId ? `super-signals:${notificationId}` : 'super-signals:update',
      renotify: false,
      data: {
        url: safeUrl,
        notification_id: notificationId,
      },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const requestedUrl = event.notification?.data?.url;
  const targetPath =
    typeof requestedUrl === 'string' && requestedUrl.startsWith('/') ? requestedUrl : '/';
  const targetUrl = new URL(targetPath, self.location.origin).href;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (new URL(client.url).origin === self.location.origin) {
          return client.focus().then(() => {
            if ('navigate' in client && client.url !== targetUrl) {
              return client.navigate(targetUrl);
            }
            return client;
          });
        }
      }
      return self.clients.openWindow(targetUrl);
    }),
  );
});
