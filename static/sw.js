// まつうランチ service worker. Its only job is to satisfy the browser's
// "installable as an app" checks so the ホーム画面に追加 button can offer a
// real install prompt. Deliberately does no offline caching or push
// handling — this is an internal ordering tool, showing stale menu/order
// state offline would be actively misleading.

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});
