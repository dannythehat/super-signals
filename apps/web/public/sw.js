const CACHE_NAME = 'super-signals-shell-v3';
const SHELL_ASSETS = ['/', '/manifest.webmanifest', '/app-icon.svg', '/super-signals-logo.png'];
const PRIVATE_PREFIXES = ['/api/', '/auth/', '/account/', '/admin/', '/owner/', '/notifications'];
const STATIC_DESTINATIONS = new Set(['style', 'script', 'image', 'font']);

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (PRIVATE_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            void caches.open(CACHE_NAME).then((cache) => cache.put('/', copy));
          }
          return response;
        })
        .catch(() => caches.match('/')),
    );
    return;
  }

  const isKnownShellAsset = SHELL_ASSETS.includes(url.pathname);
  if (!isKnownShellAsset && !STATIC_DESTINATIONS.has(request.destination)) return;

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
    typeof payload.title === 'string' && payload.title.trim() ? payload.title.trim() : 'Super Signals';
  const body = typeof payload.body === 'string' ? payload.body : 'Trade update available.';
  const rawUrl = typeof payload.url === 'string' ? payload.url : '/';
  const safeUrl = rawUrl.startsWith('/') ? rawUrl : '/';

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/app-icon.svg',
      badge: '/app-icon.svg',
      // A crash after the push service accepted a send can produce a retry. Reusing
      // the persisted notification id replaces the same visible card instead of
      // showing the user a duplicate trade alert.
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
