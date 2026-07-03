/* global clients, self */

function hermesPushData(event) {
  if (!event.data) return {};
  try {
    return event.data.json();
  } catch {
    return { body: event.data.text() };
  }
}

function hermesPushUrl(data) {
  if (typeof data.url === "string" && data.url.trim()) {
    return data.url;
  }
  if (typeof data.task_id === "string" && data.task_id.trim()) {
    return `/control/flow?task=${encodeURIComponent(data.task_id.trim())}`;
  }
  return "/control/flow";
}

self.addEventListener("push", (event) => {
  const data = hermesPushData(event);
  const title = typeof data.title === "string" && data.title.trim()
    ? data.title.trim()
    : "Hermes Control";
  const body = typeof data.body === "string" ? data.body : "";
  const tag = typeof data.tag === "string" && data.tag.trim()
    ? data.tag.trim()
    : "hermes-control";
  const url = hermesPushUrl(data);

  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag,
    data: { url },
    renotify: true,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const rawUrl = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : "/control/flow";
  const targetUrl = new URL(rawUrl, self.location.origin).href;

  event.waitUntil((async () => {
    const windows = await clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    for (const client of windows) {
      if (client.url.startsWith(self.location.origin)) {
        if ("navigate" in client) {
          await client.navigate(targetUrl);
        }
        return client.focus();
      }
    }
    return clients.openWindow(targetUrl);
  })());
});
