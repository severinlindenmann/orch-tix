// fileshare/static/js/remote-pair-ui.js: the page behind a workspace's pairing link (/remote/pair#v1...). The secret in
// the address is read and removed first (remote-pair.js readPairLink); the person sees this browser's fingerprint and
// compares it on the computer, where the owner approves or rejects (§8.1 step 5). Nothing the computer sent is drawn
// except as text.
import { loadKeys } from "./keystore.js";
import { el } from "./ui.js";
import { readPairLink, runPairing } from "./remote-pair.js";
import { loginHref } from "./nav.js";

const link = typeof document !== "undefined" ? readPairLink() : null;      // before anything else can read the address

function show(...nodes) {
  document.getElementById("remote-pair-main").replaceChildren(...nodes);
}

async function start() {
  if (!link) {
    show(el("h1", { class: "auth-title" }, "This is not a pairing link"),
      el("p", { class: "muted" }, "Make a new link on the computer (its Remote tab, Pair a device) and open it here."));
    return;
  }
  const keys = await loadKeys().catch(() => null);
  if (!keys) {
    show(el("h1", { class: "auth-title" }, "Sign in first"),
      el("p", { class: "muted" }, "The link is not kept while you sign in. Sign in, then open the link again."),
      el("a", { class: "btn btn-accent", href: loginHref() }, "Sign in"));
    return;
  }
  const label = el("input", { id: "pair-label", type: "text", maxlength: "80", value: "This browser", "aria-label": "Name for this browser" });
  const go = el("button", { class: "btn btn-accent", type: "button", id: "pair-go" }, "Pair with this computer");
  const state = el("p", { class: "muted", id: "pair-state", role: "status" });
  const fp = el("p", { id: "pair-fp", class: "mono", hidden: true });
  const fpHelp = el("p", { class: "hint", hidden: true }, "Compare this with the fingerprint on the computer before you approve there.");
  show(el("h1", { class: "auth-title" }, "Pair this browser"),
    el("p", { class: "muted" }, "The computer will show a fingerprint. Only approve it there if it matches the one shown here."),
    el("label", { class: "field" }, el("span", {}, "Name"), label), go, state, fp, fpHelp);
  const note = el("p", { class: "hint", id: "pair-cred-note", role: "status" });
  fpHelp.after(note);
  // The platform credential (Face ID, Touch ID, Windows Hello, device PIN) is made on the person's own click.
  const click = (run, again) => new Promise((resolve, reject) => {
    const t = setTimeout(() => { b.remove(); reject(Object.assign(new Error("not clicked in time"), { name: "TimeoutError" })); }, 300_000);
    const b = el("button", { class: "btn btn-accent", type: "button", id: "pair-cred", onclick: (e) => { if (!e.isTrusted) return; clearTimeout(t); b.remove(); try { resolve(run()); } catch (err) { reject(err); } } },
      again ? "Try again: register this browser" : "Register this browser with Face ID or device unlock");
    note.textContent = again ? "That took too long. Press the button again." : "This lets you confirm risky actions on this browser. It is not your passphrase and is not stored by TIX.";
    note.after(b);
  });
  go.addEventListener("click", async () => {
    go.disabled = label.disabled = true;
    try {
      const r = await runPairing({ link, label: label.value.trim() || "This browser", click,
        onCredential: (t) => { note.textContent = t; },
        onState: (t) => { state.textContent = t; },
        onFingerprint: (t) => { fp.textContent = t; fp.hidden = fpHelp.hidden = false; } });
      if (r.approved) {
        show(el("h1", { class: "auth-title" }, "Paired"), el("p", { class: "muted" }, "This browser can now open the workspace."),
          el("p", { class: "hint", id: "pair-done-cred" }, r.credential ? "Risky actions are confirmed with Face ID or device unlock on this browser." : "No device unlock is registered here, so this browser cannot type on that computer."),
          el("a", { class: "btn btn-accent", href: "/remote" }, "Open workspaces"));
      } else {
        state.textContent = r.why;
        state.setAttribute("role", "alert");
        fp.hidden = fpHelp.hidden = true;
      }
    } catch (e) {
      state.textContent = e?.name === "BridgeStorageError" ? "This browser cannot keep the keys pairing needs." : "Pairing did not work. Make a new link on the computer.";
      state.setAttribute("role", "alert");
    }
  });
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-remote-pair")) start();
