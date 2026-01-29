self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Minimal fetch handler (network-first)
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});
