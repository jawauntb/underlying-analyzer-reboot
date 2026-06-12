// Design system sandbox interactions.
// All UI behavior is purely cosmetic — no network calls.

(function () {
  "use strict";

  // ---- Click-to-copy on color swatches ----
  document.querySelectorAll("[data-copy]").forEach(function (el) {
    el.addEventListener("click", function () {
      var value = el.getAttribute("data-copy") || "";
      if (!value) return;

      var done = function () {
        el.classList.remove("copied");
        // Force reflow so the animation can replay on rapid re-clicks.
        void el.offsetWidth;
        el.classList.add("copied");
        window.setTimeout(function () {
          el.classList.remove("copied");
        }, 1300);
      };

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard
          .writeText(value)
          .then(done)
          .catch(function () {
            fallbackCopy(value);
            done();
          });
      } else {
        fallbackCopy(value);
        done();
      }
    });
  });

  function fallbackCopy(value) {
    try {
      var area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    } catch (err) {
      // Best-effort fallback — silent on failure.
    }
  }

  // ---- Mode dock pill rail (horizontal + vertical) ----
  document.querySelectorAll("[data-mode-rail]").forEach(function (rail) {
    rail.addEventListener("click", function (evt) {
      var target = evt.target;
      if (!(target instanceof HTMLButtonElement)) return;
      if (!target.hasAttribute("data-mode")) return;
      rail.querySelectorAll("button").forEach(function (btn) {
        btn.classList.remove("active");
      });
      target.classList.add("active");
    });
  });

  // ---- Loading button demo (cycles through loading + idle) ----
  document.querySelectorAll(".ds-btn[data-loading]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.hasAttribute("data-loading")) {
        btn.removeAttribute("data-loading");
        btn.removeAttribute("aria-busy");
        window.setTimeout(function () {
          btn.setAttribute("data-loading", "");
          btn.setAttribute("aria-busy", "true");
        }, 1600);
      }
    });
  });

  // ---- Local nav: highlight section in view ----
  var navLinks = Array.prototype.slice.call(document.querySelectorAll(".ds-nav a"));
  if (navLinks.length && "IntersectionObserver" in window) {
    var sectionMap = {};
    navLinks.forEach(function (link) {
      var id = (link.getAttribute("href") || "").replace(/^#/, "");
      var section = id ? document.getElementById(id) : null;
      if (section) {
        sectionMap[id] = link;
      }
    });

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          var link = sectionMap[entry.target.id];
          if (!link) return;
          if (entry.isIntersecting) {
            navLinks.forEach(function (l) {
              l.classList.remove("active");
            });
            link.classList.add("active");
          }
        });
      },
      { rootMargin: "-30% 0px -60% 0px", threshold: 0 }
    );

    Object.keys(sectionMap).forEach(function (id) {
      var node = document.getElementById(id);
      if (node) observer.observe(node);
    });
  }
})();
