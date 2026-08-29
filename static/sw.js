// まつうランチ service worker. Two jobs: (1) satisfy the browser's
// "installable as an app" checks so the ホーム画面に追加 button can offer a
// real install prompt, and (2) receive Web Push events and show them as
// notifications even when no tab is open. Deliberately does no offline
// caching — this is an internal ordering tool, showing stale menu/order
// state offline would be actively misleading.

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var payload = { title: "まつうランチ", body: "" };
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (e) {
      payload.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title || "まつうランチ", {
      body: payload.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if ("focus" in list[i]) return list[i].focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
  );
});
