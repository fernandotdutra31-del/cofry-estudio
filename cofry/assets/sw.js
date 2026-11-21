const CACHE = "cofry-studio-v6";
const SHELL = ["/studio", "/studio/assets/app.js", "/studio/assets/social.js", "/studio/assets/owner.js", "/studio/assets/generation.js", "/studio/assets/style.css",
  "/studio/assets/icon.png", "/studio/assets/icon-mask.png", "/studio/assets/manifest.webmanifest"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith("cofry-studio-") && key !== CACHE).map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});


self.addEventListener("push", event => {
  if (!event.data) return;
  let body;
  try { body = event.data.json().body; }
  catch { body = event.data.text(); }
  if (typeof body !== "string" || !body.trim()) return;
  event.waitUntil(self.registration.showNotification("Cofry", {
    body:body.slice(0,500),
    icon:"/studio/assets/icon.png",
    badge:"/studio/assets/icon.png",
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL("/studio#notificacoes",self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(async windows => {
    const studio = windows.find(client => {
      const url = new URL(client.url);
      return url.origin === self.location.origin && url.pathname === "/studio";
    });
    if (!studio) return self.clients.openWindow(target);
    const page = await studio.navigate(target);
    return (page || studio).focus();
  }));
});
