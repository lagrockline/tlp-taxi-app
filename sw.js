// Service Worker de "LDE Vols — Infos taxis"
// Rôle : recevoir les notifications push envoyées par le workflow GitHub
// Actions (via le Cloudflare Worker) et les afficher au niveau du système
// d'exploitation, même si l'app n'est pas ouverte au premier plan.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = { title: 'LDE Vols', body: 'Mise à jour de vol', tag: 'lde-vol' };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch (e) {
      payload.body = event.data.text();
    }
  }

  const options = {
    body: payload.body,
    tag: payload.tag,
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    vibrate: [250, 100, 250, 100, 250],
    renotify: true,
    data: { url: './index.html' },
  };

  event.waitUntil(self.registration.showNotification(payload.title, options));
});

// Au clic sur la notification : ouvrir l'app (ou la ramener au premier plan
// si un onglet est déjà ouvert).
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || './index.html';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
