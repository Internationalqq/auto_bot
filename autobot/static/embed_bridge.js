(function () {
  "use strict";

  if (window.parent === window) return;

  var lastScrolled = null;
  var scheduled = false;

  function publishScrollState() {
    scheduled = false;
    var root = document.scrollingElement || document.documentElement;
    var scrollTop = Math.max(Number(window.scrollY || 0), Number(root && root.scrollTop || 0));
    var scrolled = scrollTop > 24;
    if (scrolled === lastScrolled) return;
    lastScrolled = scrolled;
    window.parent.postMessage({ type: "autobot:scroll", scrolled: scrolled }, "*");
  }

  function schedulePublish() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(publishScrollState);
  }

  window.addEventListener("scroll", schedulePublish, { passive: true });
  window.addEventListener("pageshow", schedulePublish);
  document.addEventListener("DOMContentLoaded", schedulePublish, { once: true });
  schedulePublish();
})();
