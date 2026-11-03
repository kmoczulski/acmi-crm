"""Notification settings page, the service worker, and the browser's subscribe / unsubscribe / test calls (crm/push.py)."""
import json

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import push
from .models import PushSubscription
from .permissions import is_partner

SERVICE_WORKER = """// ACMI hub browser notifications (crm/push_views.py). Shows each push and opens its record when clicked.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
// Do not handle fetch. A home-screen app on iPhone goes blank when the worker answers navigations
// (the page paints empty until Safari is opened and refreshed). Push and notification clicks still work.
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'ACMI hub', {
    body: d.body || '', tag: d.tag || undefined, icon: ICON, badge: ICON, data: {url: d.url || '/', window: d.window || ''},
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const d = e.notification.data || {};
  const url = new URL(d.url || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    const tabs = list.filter(c => new URL(c.url).origin === self.location.origin);
    const tab = tabs.find(c => c.frameType !== 'nested') || tabs[0];
    // A new email opens in the hub's mail dock (static/crm/dock.js) when a hub page is already open, so nothing is
    // navigated away from; with none open, the hub opens and puts the email in a box.
    if (tab && d.window) return tab.focus().then(c => (c || tab).postMessage({type: 'hub-window', window: d.window, read: d.url}));
    if (tab) return tab.focus().then(c => (c || tab).navigate(url));
    return self.clients.openWindow(d.window ? '/?dock=' + encodeURIComponent(d.window) + '&read=' + encodeURIComponent(d.url || '') : url);
  }));
});
"""


def service_worker(request):
    """Served from the site root so it can show notifications for every page."""
    body = SERVICE_WORKER.replace("ICON", json.dumps(static("crm/logo_blue.png")))
    response = HttpResponse(body, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


def web_manifest(request):
    """The home-screen app (Add to Home Screen on an iPhone, Install on a computer): name, icon and full-screen display."""
    icons = [{"src": static(f"crm/icons/icon-light-{size}.png"), "sizes": f"{size}x{size}", "type": "image/png",
              "purpose": "any"} for size in (192, 512)]
    body = {"name": "ACMI Hub", "short_name": "ACMI Hub", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#f6f7f9", "theme_color": "#0a1a2e", "icons": icons}
    return JsonResponse(body, content_type="application/manifest+json")


def notification_settings(request):
    if request.method == "POST":
        push.save_choices(request.user, request.POST)
        messages.success(request, "Notification settings saved.")
        return redirect("crm:notification_settings")
    current = push.choices(request.user)
    rows = [{"kind": kind, "label": kind.label, "help": help, **current[kind]} for kind, help in push.KINDS]
    return render(request, "crm/notification_settings.html", {
        "rows": rows,
        "devices": PushSubscription.objects.filter(user=request.user),
        "vapid_key": settings.VAPID_PUBLIC_KEY,
        "push_ready": push.configured(),
    })


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


@require_POST
def push_subscribe(request):
    if is_partner(request.user):
        return HttpResponseForbidden("Not for partner accounts")
    data = _json(request)
    if not isinstance(data, dict):
        return HttpResponseBadRequest("Bad subscription")
    sub = push.subscribe(request.user, data, request.headers.get("User-Agent", ""))
    if sub is None:
        return HttpResponseBadRequest("Bad subscription")
    return JsonResponse({"ok": True, "device": sub.device})


@require_POST
def push_unsubscribe(request):
    data = _json(request) or {}
    endpoint = str(data.get("endpoint") or "") if isinstance(data, dict) else ""
    deleted, _ = PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True, "removed": deleted})


@require_POST
def push_device_remove(request, pk):
    get_object_or_404(PushSubscription, pk=pk, user=request.user).delete()
    messages.success(request, "That browser won't get notifications any more.")
    return redirect("crm:notification_settings")


@require_POST
def push_test(request):
    sent = push.deliver(request.user, push.payload(
        title="Test notification", body="Browser notifications from the ACMI hub are working.",
        url=reverse("crm:notification_settings")))
    return JsonResponse({"sent": sent, "ready": push.configured()})
