// "← Back to <page>" at the top of every page: the hub page you came from, kept per browser tab.
// Switching tabs or filters on the same page updates its entry rather than adding one, so Back skips to the
// previous page, not the previous tab. Uses the browser's own back when that's where it goes (keeps the scroll).
(function () {
  var box = document.getElementById("hub-back");
  if (!box) return;
  var KEY = window.top === window ? "hubTrail" : "hubTrail:side", MAX = 30;  // the split view's side keeps its own
  var here = { path: location.pathname, url: location.pathname + location.search + location.hash,
               title: (document.title || "").replace(/ · ACMI CRM$/, "").replace(/^ACMI CRM$/, "Home") };
  var trail = [];
  try { trail = JSON.parse(sessionStorage.getItem(KEY) || "[]"); } catch (e) { trail = []; }
  var top = trail[trail.length - 1];
  if (top && top.path === here.path) trail[trail.length - 1] = here;
  else if (trail.length > 1 && trail[trail.length - 2].path === here.path) trail.pop(), trail[trail.length - 1] = here;
  else trail.push(here);
  trail = trail.slice(-MAX);
  try { sessionStorage.setItem(KEY, JSON.stringify(trail)); } catch (e) { /* private window: no trail */ }
  var prev = trail[trail.length - 2];
  if (!prev) return;
  var a = document.createElement("a");
  a.href = prev.url;
  a.className = "hub-back-link";
  a.textContent = "← Back to " + (prev.title || "previous page");
  a.addEventListener("click", function (ev) {
    var ref = "";
    try { ref = new URL(document.referrer).pathname; } catch (e) { ref = ""; }
    if (ref === prev.path && history.length > 1) { ev.preventDefault(); history.back(); }
  });
  box.appendChild(a);
  box.hidden = false;
})();
