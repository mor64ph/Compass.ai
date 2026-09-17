/* Compass front-end behaviour.
 *
 * Everything lives in this one file rather than inline in the templates, so the
 * Content-Security-Policy can forbid inline script *and* eval outright — see
 * app/security.py. That removes the whole class of "injected text becomes
 * executable" bugs, which matters here because Compass renders user-supplied
 * résumé and job-description text on nearly every page.
 *
 * Templates declare behaviour with data- attributes; nothing below needs to know
 * about a specific page.
 */
(function () {
  "use strict";

  // ---- data-confirm: ask before an irreversible submit --------------------
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.getAttribute) return;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      e.preventDefault();
    }
  });

  // ---- data-loading: lock a plain-POST submit button ---------------------
  // HTMX-driven forms use data-busy-text below; this is for real navigations.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form || !form.querySelector) return;
    if (form.getAttribute("hx-post") || form.getAttribute("hx-get")) return;
    var button = form.querySelector("button[type=submit][data-loading]");
    if (!button) return;
    // Deferred a tick: the browser has already taken the submission by then, so
    // disabling the button cannot cancel it.
    setTimeout(function () {
      button.disabled = true;
      button.textContent = button.getAttribute("data-loading");
    }, 0);
  });

  // ---- data-autosubmit: submit the owning form when a control changes ----
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches("[data-autosubmit]")) return;
    var form = el.closest("form");
    if (!form) return;
    if (form.requestSubmit) {
      form.requestSubmit();
    } else {
      form.submit();
    }
  });

  // ---- htmx: per-button busy text, and a double-submit lock --------------
  function busyButton(target) {
    var form = target && target.closest ? target.closest("form") : null;
    return form ? form.querySelector("[data-busy-text]") : null;
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var button = busyButton(e.target);
    if (!button) return;
    button.setAttribute("data-idle-text", button.textContent.trim());
    button.textContent = button.getAttribute("data-busy-text");
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    var button = busyButton(e.target);
    if (button) {
      var idle = button.getAttribute("data-idle-text");
      if (idle) button.textContent = idle;
    }
    var form = e.target && e.target.closest ? e.target.closest("form") : null;
    if (form && form.hasAttribute("data-reset-on-success")) {
      if (e.detail && e.detail.successful) form.reset();
    }
  });

  // ---- chat transcripts pin to the newest message -----------------------
  // A MutationObserver rather than a <script> inside the swapped fragment,
  // because CSP forbids inline script and htmx would have to re-run it per swap.
  function pin(chat) {
    if (chat) chat.scrollTop = chat.scrollHeight;
  }

  function pinAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(scope.querySelectorAll(".chat"), pin);
  }

  var chatObserver = new MutationObserver(function () {
    pinAll(document);
  });

  function watchChats() {
    pinAll(document);
    Array.prototype.forEach.call(
      document.querySelectorAll("#intake-log, #practice-log"),
      function (host) {
        chatObserver.observe(host, { childList: true, subtree: true });
      }
    );
  }

  // ---- password confirmation, before the round trip ---------------------
  function watchPasswords() {
    var pw = document.getElementById("pw");
    var confirmField = document.getElementById("pw2");
    var warning = document.getElementById("pw-mismatch");
    if (!pw || !confirmField) return;

    function check() {
      var mismatched = confirmField.value && pw.value !== confirmField.value;
      if (warning) warning.style.display = mismatched ? "" : "none";
      confirmField.setCustomValidity(mismatched ? "Passwords do not match" : "");
    }
    pw.addEventListener("input", check);
    confirmField.addEventListener("input", check);
  }

  function init() {
    watchChats();
    watchPasswords();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
