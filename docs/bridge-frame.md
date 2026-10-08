# The dashboard frame (R10b: frame, shim and render rules)

Part of the Orch Remote work (#22, #25). This document says what the sandboxed frame, its shim and the TIX app's
frame host do and, as important, what they are not. The key scheme and the envelope are
[bridge-protocol.md](bridge-protocol.md); nothing here imports `bridge-crypto.js` or touches the mailbox. The frame
host talks to an abstract transport (below) that a later ticket implements with the real crypto and mailbox.

Status: wired (R10 wiring, #25). `/remote` (`static/remote.html`, `js/remote.js`) lists the workspaces with their presence
and opens a paired, online one in this frame; the transport is `js/remote-transport.js` (a `DeviceSession` from
`bridge-session.js`, the mailbox in `js/remote-mailbox.js`). `/remote/pair#v1...` (`js/remote-pair-ui.js`,
`js/remote-pair.js`) is the pairing ceremony of bridge-protocol.md §8.1: the secret is removed from the address first,
every answer to the `pair` request is opened tag, pin, signature, then §7, and the pin is stored only after a verified
`pending` answer whose fingerprint is the one this browser computed itself. Sign-out deletes the `fileshare-bridge`
database (`js/bridge-wipe.js`). The reply's `page` flag is read from the host's signed reply meta (`page: true`), the
one field this document asked the host to add. Streams now run end to end through the transport (below), a file or a
download from the dashboard opens in the app's own preview (`js/remote-view.js`), and the status page's Open for an online
workspace goes to `/remote?space=`. Not built: the unlock sheet and the WebAuthn ceremonies (R11). Not tested against the real orch dashboard (it lives in another repository); the test pages in
`tests/browser/test_dash_frame.py` have its shape (R0 findings, orch-core#90 and #127), and `tests/browser/test_remote.py`
drives the whole path against `tests/support/fake_bridge_host.py`, a host built on the Python reference implementation.

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

## The policy, and what it does not do

`GET /sandbox/dash?tok=<token>` answers with (one line; `<nonce>` is new for every response):

```
sandbox allow-scripts; default-src 'none'; script-src 'nonce-<nonce>'; style-src 'unsafe-inline'; img-src blob: data:; font-src blob: data:; media-src blob: data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'
```

- `sandbox allow-scripts` without `allow-same-origin`: the document has an opaque origin. No cookie, no storage, no
  IndexedDB, no access to the TIX page; no `allow-forms`, so the browser refuses any native form submission (the shim
  fires the submit event itself); no top navigation, no popups. The host also sets `sandbox="allow-scripts"` on the iframe.
- `script-src` holds one nonce and nothing else: no `'unsafe-inline'`, no `'unsafe-eval'`, no `'self'`. The only
  inline script is the shim. The dashboard's own scripts are re-created by the shim with the nonce; a script, handler or
  `eval` that the shim did not create does not run.
- `connect-src`, `frame-src` and `form-action` are `'none'`; images, fonts and media only from `blob:` and `data:`.
  `style-src 'unsafe-inline'` is needed for the dashboard's inline styles; scripts need nothing of the kind.
- **What the policy does not do.** There is no `navigate-to`: a document in the frame can still navigate its own frame
  (a link, `location = ...`), and a navigation is itself a request. The policy therefore does not by itself keep a
  foreign page out. What does is, together: the script rule below (only the dashboard's own JavaScript runs, so no
  injected markup can set `location`), the shim's interception of every anchor, the channel (a foreign document holds
  no port and so can neither talk to the host nor listen) and the heartbeat (a frame that stops answering is rebuilt).
  Do not read the policy as "nothing leaves"; a page that gets script execution can still make requests by navigating.
- The route is never cached (`Cache-Control: no-store`) and the service worker never handles it. It needs no session.
  The token must match `[A-Za-z0-9_-]{22,64}`, else 400. `infra/Caddyfile.fileshare` lets the same origin frame it
  (`X-Frame-Options: SAMEORIGIN`), like the other two sandbox pages (`tests/test_infra.py` derives the list from
  `FRAME_CSP`; this also fixes `/sandbox/widget`, which was DENY there).

## Who is talking: a channel, not a name

The origin of a sandboxed frame is the string `"null"` for every document it ever holds, and the iframe's WindowProxy
stays the same when one document replaces another, so neither `event.origin` nor `event.source` can say which document
is speaking. The frame host therefore does not trust a name; it hands the shim one `MessagePort`:

1. The TIX page chooses 24 random bytes per iframe, passes them in the URL (`?tok=`), and the route writes them into the
   document (`<html data-tok>`). The shim removes the attribute first and posts `hello {tok}` with `window.postMessage`,
   the only window message it ever sends.
2. The host accepts a window message only if `event.source` is non-null and is this iframe's window, `event.origin` is
   `"null"`, it is within the rate cap, it is a plain bounded object, it is `hello` and its token is the one it issued
   and has not spent. The token is spent by the first proof. (The token is also in the frame's own address, which the
   page can read; that is why it is one-time. A replay of it is refused.)
3. The host creates a `MessageChannel`, keeps `port1`, and transfers `port2` in `ready`, the one message that names no
   secret (it goes to `"*"`, since an opaque origin cannot be named). From then on **every message in both directions
   travels on the port**. Window messages other than a valid `hello` are dropped.
4. A document that replaces the shim's (a navigation, a page that sets `location`) never holds the port: it cannot send
   to the host, and it cannot listen, because nothing is posted to a window any more. Replies, streams and pages go only
   through the port.
5. A **heartbeat** runs over the port: the host pings every second; when two pings in a row are unanswered (about 2 s) the iframe is destroyed and a new one is built (new token, new nonce, the last page the host knows, at most
   5 rebuilds a minute, then the frame stays stopped). It does not depend on the iframe's `load` event, which a foreign
   document controls.
6. **Loads.** The host expects one load, the document it asked for. Later loads are legitimate only inside the window
   the shim opens by announcing `write` (it writes each page into the same window; that fires one more load in Chromium
   and WebKit): at most two within 1.5 s. The announcement (a message) and the load are separate tasks and may arrive in
   either order, so an unannounced load gets 150 ms of grace. Any other load rebuilds the iframe at once.

Hardening of the shim against its own realm. A page's scripts run in the same JavaScript realm as the shim and can
poison prototypes. Before any page script exists, the shim captures the port's `postMessage`, `Reflect.apply`, the
getters of `MessageEvent` (`data`, `ports`, `source`, `origin`) and the `onmessage` setter, and uses them only through the
captured `Reflect.apply`; the port lives in a closure and is never put on an object a page can reach (an event's
`target` is the port, which is why the event getters are captured too). Messages are object literals; `window.orchHost`
and its sub-objects are frozen. The page scripts run only after the port exists. `onPort` reaches its tables through captured `Map` methods; what a page
hook can still observe is the data of replies to the page's own requests (through the `Promise` it is waiting on),
which it receives anyway. It cannot obtain the port or a control message (`ping`, `go`, `copied`): a test replaces
`Map`, `Array` and `Promise` methods and the `MessageEvent` `data` getter after shim init and checks that none reach it
(`test_a_page_that_replaces_map_promise_and_array_methods...`), and another hooks `Function.prototype`, `Object`, `JSON`,
`Reflect` and `MessagePort.prototype` (`test_a_foreign_document_in_the_frame...`). A hostile document cannot poison the realm *before* the shim runs: the shim is the first
script of its document.

## The messages

On the port (no `k` field). Frame to host:

| `t` | Fields | Meaning |
| --- | --- | --- |
| `pong` | `n` | answer to `ping` |
| `req` | `id`, `gen`, `intent` (`page`, `fetch`, `asset`, `open`), `method`, `path`, `headers`, `body` (ArrayBuffer) | one request. `page`: a navigation; `open`: a link that opens elsewhere (the answer is never drawn in the frame) |
| `sopen` | `id`, `gen`, `path` | an EventSource (a GET stream) |
| `sclose`, `abort` | `id` | the page closed a stream or aborted a request |
| `write` | | the shim is about to write a page; the host cancels everything in flight and withdraws any question |
| `rendered` | `path`, `push`, `title` | a page was written; `push` when a user's click started it |
| `hist` | `op` (`push`, `replace`, `back`, `forward`, `go`), `path` or `n` | the dashboard's `pageHistory` |
| `theme` | `value` (`light`, `dark`, `system`) | `setTheme` |
| `copy` | `id`, `text` (1 to 2000 characters) | `copy`: only a question, see below |
| `open` | `href` (http or https) or `path` | a link that opens a new tab; internal ones go to the viewer |
| `download` | `path` | a `download` link |
| `log` | `m` | a script error, 200 characters at most |

Host to frame: `ping {n}`, `go {path}` (the app starts a page: first page, Back, Forward), `res {id, gen, status, headers,
url, body}`, `page {id, gen, path, html}`, `handled {id, gen, outcome}`, `err {id, gen, code, message}`, `sdata {id, gen,
chunk}`, `send {id, gen}`, `copied {id, ok}`. On the window: `hello {k, t, tok}` (frame to host) and `ready {k, t}` with the
port (host to frame).

## What the host checks

Before anything reads a field, `boundedShape` (the browser has already cloned the message; a hostile frame can make that
cost something and nothing here can stop it, so what is bounded is what the host does with it): a plain object (not an
array, a Map or an object with an inherited prototype), at most 12 own keys, a string `t` of at most 16 characters, no
string value over 4096 characters. Then `frame-scope.js` `checkRequest` runs on every `req` and `sopen` before the
transport sees it:

- `validPath`: one leading slash, then printable ASCII only (everything else percent-encoded), at most 2048
  characters, no `//` or `\` at the start, no empty segment, no `#`, no `.` or `..` segment (also encoded), no encoded slash,
  backslash or NUL, no percent-escape of a character that needs none (`/%61/x`). Never-a-page matching works on a
  canonical form (lower case, no repeated or trailing slash).
- Method in the known list; `asset` and `open` are GET; `page` is GET or POST; a body only on methods that carry one,
  at most 1 MiB, and as bytes.
- Headers: only `accept`, `content-type`, `if-none-match` survive; a bad value refuses the request. `Cookie`, `Origin`
  and `Host` never leave the frame.
- The injected **scope table** (advisory only: the host decides again from its own router): rules
  `{methods, pattern, stream?}`, pattern segments literal, `:name` (one segment) or a trailing `*`. Every intent, assets
  included, needs a rule that names its method and path; a stream needs a rule with `stream: true`. The path an answer
  finally came from (`head.url`, after any redirect the host followed) is checked the same way.
- Caps per frame (`LIMITS` in `frame-host.js`): 16 requests in flight, 4 streams, 200 messages per second (the excess
  is dropped; 3 seconds in a row over the cap destroys the frame), 8 MiB per buffered answer, 4 MiB per page.

A refusal answers `err` with a fixed code (`shape`, `path`, `method`, `scope`, `size`, `busy`), never an echo of what
the frame sent.

## Gestures, questions and what needs a person

Everything that leaves the frame to somewhere else needs the person, in the TIX page (`navigator.userActivation.isActive`,
which user activation in a frame inside the page switches on; without the API nothing that needs a gesture happens, and at
most one gated action per second):

- **Opening a file in the viewer**, **an outside address** and **a download** need a gesture. An outside address and a
  download are then shown to the person in a question (below) and happen only on a click on its button. Downloads show
  the name and size. The default external action is `window.open(url, "_blank", "noopener,noreferrer")`.
- **History.** `back`, `forward` and `go` need a gesture and stay inside the entries the frame itself pushed (the app
  keeps its own history); a push without a gesture is a replace.
- **The clipboard.** The frame can only ask (`copy`). The question shows the exact text, at most 2000 characters, as text
  with hidden and bidirectional characters made visible (`ui.js` `shown`), in a box that is fixed on screen and placed
  after the frame in the document, so it never moves the frame under the pointer. Its button is disabled for the first
  500 ms and only a real click on it (`isTrusted`) counts, never a click in the frame. One question at a time, 2 seconds
  apart, withdrawn after 30 seconds, when the page changes or when the frame is rebuilt. There is no clipboard read.

## What is written into the frame

Only a response the **host** tagged as a dashboard page (`head.page === true`, a field of the transport's head event
that a later ticket fills from the host's signed reply; never read from the body or from a header the page controls)
that is 200 `text/html` and whose final path is not one of the routes that carry their own policy (`/a/*`, `/w/*`,
`/wp/*`, `/wpf/*`, `/addons/:name/files/*`, `/t/:ref/raw`) is written (`frame-render.js` `classify`). The dashboard
sends its own Content-Security-Policy on every page, so "no policy of its own" is not the test. Everything else:

- 200 answers of other types, an untagged HTML answer or an artifact go to the viewer callback (the existing sandboxed
  viewer, wired later; gesture needed);
- `attachment`, `application/octet-stream` or no type are offered as a download (gesture and a click on the question);
- anything not 2xx becomes a one-line notice drawn with `textContent`.

Nested frames (an artifact, a widget) are replaced by a link, "Open ... in the viewer". On the Remote page the
viewer does not exist for the routes that carry their own policy (`isNeverPage`): the link shows the notice "Agent
widgets and artifacts are not available remotely. Open them on the computer." (`remote.js`) instead of a blank frame
or an error. Host-supplied strings (names,
error pages, titles) are drawn as text; `tests/js/frame-scope.test.mjs` checks that the host modules contain no
`innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`.

## What the shim does

- Replaces `fetch`, `XMLHttpRequest`, `EventSource`, `window.open`, link clicks and form submits with messages. A
  cross-origin URL fails in the page. Because the sandbox has no `allow-forms`, the browser never fires `submit`; the
  shim fires it for a submit button, Enter in a field and `requestSubmit` (once, also in a browser that fires it
  itself), so the page's own handlers still run.
- **Links.** The click handler takes any anchor on the event's path, in any namespace and whatever its attributes (HTML
  `a`, `area`, SVG `a` with `href` or `xlink:href`, an animated href), prevents the default and sends what it resolves
  to through the host; keyboard activation is a click. A page script that stops propagation before the handler can still
  let a native navigation through; the channel and the heartbeat are what contain that.
- Supplies `window.orchHost` (orch-core's adapter, PR #118) before any page script: `path`, `search`, `url`, `hash`,
  `resolve`, `navigate`, `reload`, `pageHistory`, `setTheme`, `session` and `local` (replaced as wholes, in memory),
  `copy`, `openLink`, `download`; frozen. In-memory stand-ins for `cookie`, `localStorage`, `sessionStorage`. The page's
  address is the shim's own `current`; the frame's real URL (it carries the token) is never consulted.
- Writes a page. The page is parsed inertly (`DOMParser`), changed as a DOM and moved into the live document with
  `importNode` after a fixed empty skeleton: it is never serialised and parsed a second time, so the cleaning parser and
  the rendering parser cannot disagree. Removed: `base`, `object`, `embed`, `meta http-equiv`, `noscript`, every script in
  any namespace and in templates, SMIL (`animate`, `set`, `animateMotion`, `animateTransform`), every `on*`, `srcdoc`,
  `ping` and `nonce` attribute, every `javascript:`, `vbscript:` and `data:text/html` URL (after dropping whitespace and
  control characters), on SVG and MathML elements every `href` or `xlink:href` that is not an own-origin address or a
  fragment (and every `use` reference that is not a fragment), and **every `link` that is not a stylesheet** (an
  allow-list: `dns-prefetch`, `preconnect`, `prerender`, `next` and the rest would reach out). Stylesheets become
  `<style>` (CSS `url()` into blob URLs, `@import` removed), images and media sources become blob URLs, iframes become
  viewer links.
- **Scripts.** A classic `script src` runs only when (1) its path is under `/static/` (a constant in the shim, the
  dashboard's own static route, never taken from the page), (2) the answer's content type is a JavaScript type (the shim
  fetched the bytes, so the nosniff check is the shim's), and (3) the file's SHA-256 starts with the `?v=` of its URL.
  **(3) is cache busting plus detection of an accidental mismatch, nothing more**: the page writes both the URL and the
  stamp, so against hostile markup it adds nothing. (1) and (2) are what keep an artifact, an API response or any other
  file from running as a script. Inline scripts, module scripts and `importmap` do not run. Each script is re-created with
  the nonce and removed right after it ran.
- Announces `write`, **closes all open streams**, rejects everything pending, bumps the page generation, writes the page.
- **Generations.** Every request carries the page generation (`gen`); every answer echoes it. An answer for an older
  page is dropped and its promise rejected; of two navigations only the newest is written.
- **Asset cache.** Scripts, styles, images and fonts are cached by URL (at most 128 entries or 24 MiB).

## The transport interface

`bridge-transport.js` documents it: `transport.request(req)` returns an async iterable of events,
`{type:"head", status, headers, url?, page?}` once, `{type:"chunk", data}` any number, `{type:"end"}`. `req` is
`{method, path, headers, body, stream, signal}`; the implementation stops when `signal` aborts. A transport failure is a
thrown `Error` whose message is shown as text. `fakeTransport(answer)` is the test fake. The real implementation follows
redirects itself, reports the final path in `head.url`, and sets `head.page` from the host's signed reply.

## What the frame host guarantees about streams

For the dashboard's own code (the terminal page posts its keys while its stream is open, so this is what a typing lease
needs from the device side). `js/frame-host.js` and `js/remote-transport.js` together:

- **A stream the page opens stays open** until the page closes it, the page changes (`write`), the workspace is switched, the
  computer ends it (`LAST`), the computer is lost, or a refusal ends it. Showing the terminal page does not close it, and
  nothing the host does on its own does (the heartbeat rebuilds a frame that stopped answering, which is a page change).
- **Frames reach the page in order**, each chunk as its own `sdata`; the shim joins chunks and splits coalesced ones into
  events. A keepalive is not a frame. `LAST` ends the stream (`send`: the page's `EventSource` sees an error and is closed).
- **`cancel` goes to the computer** whenever the page side lets go: `sclose`, a page change, a workspace switch (the old
  mailbox is kept for 10 s so the cancel is answered), silence, or a consumer that is too slow. It is best effort and a
  request of its own.
- **Silence is 40 s.** The computer sends a keepalive at least every 20 s; no chunk at all for 40 s (two missed) says "The
  computer did not answer", cancels and ends the stream. The next answer of any kind clears the note.
- **Reconnects back off.** Every reconnect is a bridged request and the host's quota is about 1.1 requests a second. A new
  stream for a path waits until 10 s after the previous one for that path started; each stream that dies young (under 60 s)
  doubles the wait: 10, 20, 40, 60 s. One that lived a minute starts it over. A stream the page closed itself changes
  nothing. The path is taken without its query string (64 paths are remembered), and once any stream has failed no stream
  for any path opens less than 2 s after the previous one. The wait is shown ("Reconnecting to the computer in N s") and costs no request. The page's own reconnect code is
  held to this too, because the gate is in the transport. A stream whose answer is not a 200 `text/event-stream` is cancelled at the
  computer.
- **After a refusal that ends a stream for good** (`revoked`, `not_paired`, `stopped`, `scope_changed`) this transport opens
  no stream again (zero requests); the fixed text is shown. Other refusals (`busy`) are not final.
- **Backpressure.** More than 256 chunks waiting for a consumer that is not reading drops the stream, cancels it and leaves
  the reconnect to the back-off above.
- **Keys for a terminal** (`POST /terminals/{name}/keys`, `{n, page, ...}`) are ordinary page requests from the dashboard's
  script; the lease needs the stream the same device opened to be open when they go out, and the above keeps it open.

## The viewer and the download

`viewer` and `download` are `js/remote-view.js`. A file answer (not a tagged dashboard page) needs a gesture first
(`frame-host.js`); a download is then shown in a question (name and size) and saved on its button. The viewer uses the file
view's renderers (`render.js`): text through `textContent`, JSON as text, an image or audio file from a blob URL, Markdown
as text (the renderer library belongs to the files page). **HTML and SVG are never shown**, by name or by type
(`previewKind`); the person can only save them. The panel is headed "From the dashboard: <name>" and the download question shows the same sanitised name that is saved; an audio type is used only as a plain token. Caps: 2 MiB for text, 8 MiB for an image or audio file, which is also the
frame host's whole answer cap and the download cap. Nothing of the file runs; the panel holds no script and no frame.

## Limits you should know

- The shim and the dashboard page share one JavaScript realm. A hostile page can poison prototypes after the shim has
  captured what it needs (above); it can still call the shim's public wrappers (`fetch`, `orchHost`), which is exactly
  what the scope table, the caps and the gestures limit.
- A page that is already running (the dashboard's own script, or one an attacker got past the script rule) can read the
  frame's address, which holds the spent token, and can navigate the frame; it then holds no port (above).
- Module scripts and `<script type=importmap>` are not supported (stripped). A native `Location` change cannot be
  intercepted; it rebuilds the frame. `history.pushState` throws in an opaque origin; the adapter is the way.
- Tested in Chromium and WebKit (Playwright); not in Firefox (no build available locally). The real
  user-activation API is tested in Chromium only (needs a DevTools session); other tests replace the host's activation
  test, because the driver's own scripts switch real activation on.
- A 3xx never reaches the frame: the transport follows redirects.

## The AI Factory (R13, #27)

The Factory pages (permission cards, the epic's Pause, Stop and Start, the Ready and Stopped reports, the verdict) are
the dashboard's own pages, drawn in this frame; their buttons post urlencoded forms through the same transport. When the
host answers `assertion_required` the unlock sheet shows the host's exact subject text and the very same form body is
sent once more inside the `assert` request. Deny, Revoke and Pause need no sheet (Decide). The app adds only status: the
workspace card (status page and `/remote`) names the Factory code from the heartbeat (running, paused, waiting on a
permission, ready, stopped, done) with children done and budget used, a waiting Factory gets a link that opens the
workspace in the frame (`/remote?space=...&path=%2F`; `path` is checked by `validPath`, anything else opens `/`), and a
lost host shows "host lost: nothing new starts and no parked child wakes until it is back" with no claim about sessions
already running. Tested against the fake host (`tests/browser/test_factory.py`); a real phone and the real dashboard's
pages are not covered.
