// Service worker de l'app mobile ParcInfo (lecture seule).
//
// Volontairement minimal : ne met en cache que l'habillage (icônes), jamais
// les pages ou données du parc — celles-ci contiennent des informations
// sensibles (identifiants, coordonnées) qui ne doivent pas persister dans
// le Cache Storage d'un téléphone partagé ou perdu. Le seul rôle de ce
// fichier est de satisfaire les critères d'installabilité PWA et de servir
// les icônes plus vite ; toute page reste chargée depuis le réseau.
const CACHE_NAME = 'parcinfo-mobile-shell-v1';
const SHELL_ASSETS = ['/static/icon.png', '/static/icon_1024.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (SHELL_ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((cached) => cached || fetch(req)));
  }
  // Toute autre requête (pages, API) part directement au réseau.
});
