"use strict";

// The admin surface contains private, live host state. Keep the PWA installable
// without placing authenticated pages, API responses, or tool data in a browser
// cache. Every navigation and request therefore remains network-backed.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});
