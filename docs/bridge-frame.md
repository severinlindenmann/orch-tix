# The dashboard frame (R10b: frame, shim and render rules)

Part of the Orch Remote work (#22, #25). This document says what the sandboxed frame, its shim and the TIX app's
frame host do and, as important, what they are not. The key scheme and the envelope are
[bridge-protocol.md](bridge-protocol.md); nothing here imports `bridge-crypto.js` or touches the mailbox. The frame
host talks to an abstract transport (below) that a later ticket implements with the real crypto and mailbox.

Status: built against the fake transport. **Not wired into any page yet**: the unlock sheet and the wiring step import
`frame-host.js`. Not tested against the real orch dashboard (it lives in another repository); the test pages in
`tests/browser/test_dash_frame.py` have its shape (R0 findings, orch-core#90 and #127).

## What is where

| Piece | File | Runs in |
| --- | --- | --- |
| Frame route and policy | `fileshare/routes/pages.py` `sandbox_dash`, `fileshare/headers.py` `DASH_CSP`, `FRAME_CSP` | server |
| Frame document | `fileshare/static/sandbox-dash.html` | the iframe |
| Shim | `fileshare/static/js/frame-shim.js` (inline in the frame document, nonce) | the iframe |
| Frame host | `fileshare/static/js/frame-host.js` | the TIX page |
| Message validation and the scope table | `fileshare/static/js/frame-scope.js` | the TIX page |
| Render rules | `fileshare/static/js/frame-render.js` | the TIX page |
| Transport interface and a fake | `fileshare/static/js/bridge-transport.js` | the TIX page |

## The policy

`GET /sandbox/dash?tok=<token>` answers with (one line; `<nonce>` is new for every response):

```
sandbox allow-scripts; default-src 'none'; script-src 'nonce-<nonce>'; style-src 'unsafe-inline'; img-src blob: data:; font-src blob: data:; media-src blob: data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'
```

- `sandbox allow-scripts` without `allow-same-origin`: the document has an opaque origin. No cookie, no storage, no
  IndexedDB, no access to the TIX page; no `allow-forms`, so the browser refuses any native form submission (the shim
  fires the submit event itself); no top navigation. The host also sets `sandbox="allow-scripts"` on the iframe.
- `script-src` holds one nonce and nothing else: no `'unsafe-inline'`, no `'unsafe-eval'`, no `'self'`. The only
  inline script is the shim, which the route puts into the document with this load's nonce. Pages' own scripts are
  re-created by the shim with the nonce; a script, handler or `eval` that the shim did not create does not run.
- Nothing leaves: `connect-src`, `frame-src`, `form-action` are `'none'`; images, fonts and media only from `blob:`
  and `data:` (the shim turns the page's files into blob URLs). `style-src 'unsafe-inline'` is needed for the
  dashboard's inline styles; scripts need nothing of the kind.
- The route is never cached (`Cache-Control: no-store`) and the service worker never handles it. It needs no session.
  The token must match `[A-Za-z0-9_-]{22,64}`, else 400. `infra/Caddyfile.fileshare` lets the same origin frame it
  (`X-Frame-Options: SAMEORIGIN`), like the other two sandbox pages (`tests/test_infra.py` derives the list from
  `FRAME_CSP`).

## Who is talking

The origin of a sandboxed frame is the string `"null"` for every document it ever holds, so the origin proves nothing
by itself. The frame host accepts a message only when all of these hold (`frame-host.js` `onMessage`):

1. `event.source` is this iframe's window; any other window is ignored without being counted.
2. `event.origin` is `"null"`.
3. It is within the per-frame rate cap (below).
4. Shape: an object with `k: "orch-frame-1"` and a string `t`.
5. Either it is `hello` carrying the one-time token (below), or it carries the session id (`sid`) the host answered
   `hello` with. A message without it is dropped.

The token. The TIX page chooses 24 random bytes per iframe, passes them in the URL, and the route writes them into
the document (`<html data-tok>`). The shim removes the attribute first and answers `hello {tok}`. The host spends the
token on the first matching `hello` (a second one is refused, also with the same value), then answers
`ready {sid}` with a fresh 24-byte session id. The shim keeps `sid` in its closure; a later document in the same
window (a self-navigated page) has neither token nor `sid`. The host replies only after this handshake, and holds
every reply while a load it did not vouch for is unproven.

Loads. The host expects exactly one load (the document it asked for). Later loads are legitimate only inside the
window that the shim opens by announcing `write` (it writes each page into the same window, which fires one more
load in Chromium and WebKit): at most two within 1.5 s (the announcement and the load are separate tasks and may arrive in either order, so an unannounced load gets 150 ms of grace for its announcement; nothing is trusted meanwhile). After each of those the host sends `ping`, and the frame is
trusted again only when the shim answers `pong` with the session id. **Any other load, a missing hello after 6 s or a
missing pong after 1.5 s destroys the iframe and builds a new one** (new token, new nonce, the last page the host
knows, at most 5 rebuilds a minute, then the frame stays stopped). This is what makes a page that navigates its own
frame (a link the shim did not intercept, `location = ...`, a reload) harmless: the new document has no session.

## The messages

All messages carry `k: "orch-frame-1"`. Frame to host (each with `sid` after `ready`):

| `t` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `tok` | proof of the one-time token |
| `pong` | `n` | answer to `ping` |
| `req` | `id`, `gen`, `intent` (`page`, `fetch`, `asset`, `open`), `method`, `path`, `headers`, `body` (ArrayBuffer) | one request. `page`: a navigation; `open`: a link that opens elsewhere (answer never drawn in the frame) |
| `sopen` | `id`, `gen`, `path` | an EventSource (a GET stream) |
| `sclose`, `abort` | `id` | the page closed a stream or aborted a request |
| `write` | | the shim is about to write a page; the host cancels everything in flight |
| `rendered` | `path`, `push`, `title` | a page was written; `push` when a user's click started it |
| `hist` | `op` (`push`, `replace`, `back`, `forward`, `go`), `path` or `n` | the dashboard's `pageHistory` |
| `theme` | `value` (`light`, `dark`, `system`) | `setTheme` |
| `copy` | `id`, `text` (1 to 2000 characters) | `copy`: only a question, see "The clipboard" |
| `open` | `href` (http or https) or `path` | a link that opens a new tab; internal ones go to the viewer |
| `download` | `path` | a `download` link |
| `log` | `m` | a script error, 200 characters at most |

Host to frame: `ready {sid}`, `ping {n}`, `go {path}` (the app starts a page: first page, Back, Forward), `res {id, gen,
status, headers, url, body}`, `page {id, gen, path, html}`, `handled {id, gen, outcome}` (the answer went to the
viewer, a download or a notice), `err {id, gen, code, message}`, `sdata {id, gen, chunk}`, `send {id, gen}`,
`copied {id, ok}`.

## What the host checks

`frame-scope.js` `checkRequest` runs on every `req` and `sopen` before the transport sees it:

- `validPath`: one leading slash, then printable ASCII only (everything else percent-encoded), at most 2048
  characters, no `//` or `\` at the start, no `#`, no `.` or `..` segment (also encoded), no encoded slash, backslash
  or NUL.
- Method in the known list; `asset` and `open` are GET; `page` is GET or POST; a body only on methods that carry one,
  at most 1 MiB, and as bytes.
- Headers: only `accept`, `content-type`, `if-none-match` survive; a bad value (control characters, over 512
  characters, more than 16 headers) refuses the request. `Cookie`, `Origin` and `Host` never leave the frame.
- The injected **scope table** (advisory only: the host decides again from its own router): rules
  `{methods, pattern, stream?}`, pattern segments literal, `:name` (one segment) or a trailing `*`. A request needs a
  rule that names its method and path; a stream needs a rule with `stream: true`. The path the answer finally came from
  (`head.url`, after any redirect the host followed) is checked the same way.
- Caps per frame (`LIMITS` in `frame-host.js`): 16 requests in flight, 4 streams, 200 messages per second (the excess
  is dropped; 3 seconds in a row over the cap destroys the frame), 8 MiB per buffered answer, 4 MiB per page.

A refusal answers `err` with a fixed code (`shape`, `path`, `method`, `scope`, `size`, `busy`), never an echo of what
the frame sent.

## What is written into the frame

Only a response the **host** tagged as a dashboard page (`head.page === true`, a field of the transport's head event
that a later ticket fills from the host's signed reply; never read from the body or from a header the page controls)
that is 200 `text/html` and whose final path is not one of the routes that carry their own policy (`/a/*`, `/w/*`,
`/wp/*`, `/wpf/*`, `/addons/:name/files/*`, `/t/:ref/raw`) is written (`frame-render.js` `classify`). The dashboard
sends its own Content-Security-Policy on every page, so "no policy of its own" is not the test. Everything else:

- 200 answers of other types, an untagged HTML answer or an artifact go to the viewer callback (the existing sandboxed
  viewer, wired later);
- `attachment`, `application/octet-stream` or no type go to the download callback;
- anything not 2xx becomes a one-line notice drawn with `textContent`.

Nested frames (an artifact, a widget) are replaced by a link, "Open ... in the viewer", that opens the path in the
viewer; the frame has `frame-src 'none'` anyway. Host-supplied strings (names, error pages, titles) are drawn as
text; `tests/js/frame-scope.test.mjs` checks that the host modules contain no `innerHTML`, `outerHTML`,
`insertAdjacentHTML` or `document.write`. The shim's one `document.write` writes the page it rebuilt from an inert
parse (below).

## The clipboard

The frame can only ask. A `copy` message shows the full text (at most 2000 characters) in the TIX page, as text, with
Copy and Dismiss buttons; it is written to the clipboard only by a click on Copy there, which is a real gesture in
the TIX page. One question at a time, 2 seconds apart, withdrawn after 30 seconds, when the page changes or when the
frame is rebuilt; nothing is accepted while a load is unproven. There is no clipboard read and no message for one.

## What the shim does

- Replaces `fetch`, `XMLHttpRequest`, `EventSource`, `window.open`, link clicks and form submits with messages. A
  cross-origin URL fails in the page. Because the sandbox has no `allow-forms`, the browser never fires `submit`; the
  shim fires it for a submit button, Enter in a field and `requestSubmit` (once, also in a browser that fires it
  itself), so the page's own handlers still run.
- Supplies `window.orchHost` (orch-core's adapter, PR #118) before any page script: `path`, `search`, `url`, `hash`,
  `resolve`, `navigate`, `reload`, `pageHistory`, `setTheme`, `session` and `local` (replaced as wholes, in memory),
  `copy`, `openLink`, `download`. In-memory stand-ins for `cookie`, `localStorage`, `sessionStorage`. The page's
  address is the shim's own `current`; the frame's real URL (it carries the token) is never consulted.
- Writes a page: parse inertly (`DOMParser`), strip `base`, `object`, `embed`, `meta http-equiv`, icons, preloads, `noscript`, every script in any namespace (svg, math,
  templates), every `on*`, `srcdoc`, `ping` and `nonce` attribute, every `javascript:`, `vbscript:` and `data:text/html` URL
  (after dropping whitespace and control characters), every inline script and every module script; re-create each classic same-origin `script src` from
  fetched text, **only when the SHA-256 of the file starts with the `?v=` of its URL** (the dashboard's content stamp;
  a script without a valid `?v=` does not run); turn stylesheets into `<style>` (CSS `url()` into blob URLs,
  `@import` removed), images and media sources into blob URLs; replace iframes by viewer links. It then announces `write`,
  **closes all open streams**, rejects everything pending, bumps the page generation, writes a fixed empty skeleton, moves the cleaned page in as nodes (`importNode`: it is never serialised and parsed a
  second time, so the cleaning parser and the rendering parser cannot disagree), appends the
  scripts with the nonce in order (each removed after it ran, so nothing in the DOM carries the nonce) and closes the document.
- **Generations.** Every request carries the page generation (`gen`); every answer echoes it. An answer for an older
  page is dropped and its promise rejected; of two navigations only the newest is written.
- **Asset cache.** Scripts, styles, images and fonts are cached by URL (at most 128 entries or 24 MiB); the second
  page costs a request or two.

## The transport interface

`bridge-transport.js` documents it: `transport.request(req)` returns an async iterable of events,
`{type:"head", status, headers, url?, page?}` once, `{type:"chunk", data}` any number, `{type:"end"}`. `req` is
`{method, path, headers, body, stream, signal}`; the implementation stops when `signal` aborts. A transport failure is a
thrown `Error` whose message is shown as text. `fakeTransport(answer)` is the test fake. The real implementation follows
redirects itself, reports the final path in `head.url`, and sets `head.page` from the host's signed reply.

## Limits you should know

- The shim and the dashboard page share one JavaScript realm. A hostile page can poison prototypes, so the shim is not a
  security boundary; the boundary is the opaque origin, the policy and the host-side validation above. The shim's
  `sid` lives in a closure; a page that can read it already runs inside the frame and can do what the shim can, which
  is exactly what the scope table limits.
- The frame's nonce is exposed to script as `script.nonce` while a script element exists; the shim removes its script
  elements right after they ran. A page that is already running can create one anyway, as it could run code anyway.
- Module scripts and `<script type=importmap>` are not supported (stripped). A native `Location` change cannot be
  intercepted; it destroys the frame (above). `history.pushState` throws in an opaque origin; the adapter is the way.
- Tested in Chromium and WebKit (Playwright); not in Firefox (no build available locally).
- A refused load of a 3xx never reaches the frame: the transport follows redirects.
