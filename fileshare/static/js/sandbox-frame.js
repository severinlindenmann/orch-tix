// fileshare/static/js/sandbox-frame.js — the /sandbox/html frame (spec T15). The parent posts
// {type: "tix-render", html} once, after this frame's load event, with targetOrigin "*" (the
// frame's own origin is opaque, from the sandbox CSP directive). This validates that message and
// renders the HTML exactly once; it never posts anything back to the parent.
//
// A CLASSIC script, deliberately: from an opaque (sandboxed) origin, the browser treats a
// `type="module"` fetch as cross-origin and blocks it (no CORS headers can help — the origin is
// `null`), so nothing would ever load. No import/export here for the same reason. The whole body is
// an IIFE, so nothing lands on the frame's `window` for the attachment's own script to find or
// replace. Only where there is no `window` (Node's tests load this with `vm`) does it expose
// `globalThis.__tixSandbox = {makeAcceptor, renderInto}`.
(function tixSandboxFrame() {
  "use strict";

  // A factory, not a singleton, so each caller (a real frame, or one test case) gets its own
  // "already rendered" state instead of sharing module-level mutable state.
  function makeAcceptor() {
    let rendered = false;
    return function acceptRender(event, parentWin) {
      if (rendered) return null;
      if (!event || event.source !== parentWin) return null;
      const data = event.data;
      if (!data || typeof data !== "object" || data.type !== "tix-render") return null;
      if (typeof data.html !== "string") return null;
      rendered = true;
      return data.html;
    };
  }

  function renderInto(win, html) {
    win.document.open();
    win.document.write(html);
    win.document.close();
  }

  if (typeof window === "undefined") {
    globalThis.__tixSandbox = { makeAcceptor, renderInto };
    return;
  }
  const acceptRender = makeAcceptor();
  window.addEventListener("message", (event) => {
    const html = acceptRender(event, window.parent);
    if (html !== null) renderInto(window, html);
  });
})();
