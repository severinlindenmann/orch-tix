// fileshare/static/js/needs.js — the Needs you tab (/) and the Tickets tab (/?view=tickets) of the
// phone (TIX on orch-core, spec §10). Needs you: one section per workspace with the mirrors whose
// cleartext `needs` is set, newest first, and the desktop's last-seen line. Tickets: every linked
// ticket by workspace, each with the shared move chip and progress strip (ticket-card.js). A card opens
// /t/<n>#answer. A Needs you card is a decision card: one open single-choice question is answered in place
// (numbered 48 px options, the recommended one marked; a tap arms, Send answer sends, nothing commits on the
// first tap); an approval or a verdict opens the ticket's read-and-decide view. Nothing needs you: a calm
// inbox-zero card. Pending join requests show as a banner to Settings.
// The last-known lists (mirrors-data.js) render at once, then the network's answer replaces them;
// offline, the cached list stays with "Offline — as of HH:MM".
import { api } from "./api.js";
import { shortAge } from "./format.js";
import { el, icon, shown, toast } from "./ui.js";
import { cacheLabels, keysOrLogin, loadCachedLists, loadDecisions, loadMirrors, loadSpaces, signInAgain, watchMirrors } from "./mirrors-data.js";
import { loadMessages, messagesSection, ticketRequestCard } from "./phone-inbox.js";
import {
  NEEDS_LABEL, NEEDS_PILL, NOT_YET_MS, QUEUED_TEXT, UNKNOWN_SPACE, outcomeRole, outcomeText, approvalGate, canSendAnswer, cardTitle, decisionValue, groupBySpace,
  lastSeenText, needsYou, questionCount, targetFor,
} from "./mirror-model.js";
import {
  blockingCount, cardAction, cardLine, gateImage, moveChip, progressStrip, proofImage, quickAnswer, taskBar, taskChip,
} from "./ticket-card.js";

// " · idle 40d" after a board row's title: an open or backlog ticket nobody touched for a while (schema 1.7)
const idleTag = (doc) => (Number.isInteger(doc?.revalidate?.idle_days) && doc.revalidate.idle_days > 0
  ? el("span", { class: "muted", title: "Untouched for a while — check it still holds" }, ` · idle ${doc.revalidate.idle_days}d`) : null);
import { pinnedFigure } from "./images.js";
import { OutboxFullError, queuedDecisions } from "./outbox-ui.js";
import { myDecisions, sendDecisions } from "./decision-send.js";
import { pairingFor } from "./pairing.js";
import { openDecision } from "./mirror-crypto.js";
import { hexToBytes } from "./crypto.js";

// A ticket's status: never pink (role "you" is only for what needs the human, the needs pill).
export const STATUS = {
  backlog: ["neu", "ring", "Backlog"], open: ["info", "ring", "Open"], "in-progress": ["info", "half", "In progress"],
  waiting: ["warn", "clock", "Waiting"], testing: ["info", "half", "Testing"], done: ["ok", "check", "Done"],
};

export function pill(role, iconName, text) {
  return el("span", { class: `pill r-${role}` }, icon(iconName), el("span", {}, text));
}

export function needsPill(row) {
  const n = row.needs === "question" ? row.open_questions : 0;
  return pill("you", "dot", n ? `${NEEDS_PILL[row.needs]} · ${n}` : NEEDS_PILL[row.needs] || "Needs you");
}

export function statusPill(status) {
  const [role, ic, text] = STATUS[status] || ["neu", "ring", status || "Unknown"];
  return pill(role, ic, text);
}

// The shared ticket-card encoding: the move chip (pink only when it is your move) and the progress strip.
export function moveChipEl(row, doc) {
  const m = moveChip(row, doc);
  return pill(m.role, m.icon, m.text);
}

export function stripEl(doc) {
  const steps = progressStrip(doc);
  if (!steps.length) return null;
  return el("ul", { class: "strip", "aria-label": "Progress" }, steps.map((s) =>
    el("li", { class: `strip-chip r-${s.role}`, title: s.label }, el("span", { "aria-hidden": "true" }, s.text), el("span", { class: "sr-only" }, s.label))));
}

// The Needs you card's chip: the verb of your move.
function needsChip(row, doc) {
  if (row.needs === "approval") return pill("you", "dot", `Approve ${approvalGate(doc) || "plan"}`);
  if (row.needs === "verdict") return pill("you", "dot", "Verdict");
  if (row.needs === "question") return pill("you", "dot", row.open_questions > 1 ? `Answer · ${row.open_questions}` : "Answer");
  return needsPill(row);
}

// ---- the one-tap answer on a card. Its state outlives a re-render of the list (a reload, the long-poll):
// the armed option, the decision ids (a retry is the same decision), the receipt.
const quick = new Map();

function quickAnswerEl(row, doc, qa) {
  const q = qa.question;
  const slot = `${row.id}:${q.hash}`;
  const st = quick.get(slot) || { armed: null, ids: {}, busy: false, status: null };
  quick.set(slot, st);
  // the recommended option first (v4), each keeping its own number
  const ordered = [...qa.options.filter((o) => o.rec), ...qa.options.filter((o) => !o.rec)];
  const opts = ordered.map((o) => el("button", { type: "button", class: "btn qa-opt", "aria-pressed": String(st.armed === o.key),
    dataset: { key: o.key, fkey: `qa:${slot}:${o.key}` } }, el("span", { class: "qa-label" }, `${o.n} · `, shown(o.label)),
    o.rec ? el("span", { class: "pill r-ok qa-rec" }, icon("check"), el("span", {}, "recommended")) : null,
    o.cost ? el("span", { class: "dq-cost" }, String(o.cost)) : null));
  const sendBtn = el("button", { type: "button", class: "btn btn-primary btn-big", dataset: { fkey: `qa-send:${slot}` } }, "Send answer");
  const cancel = el("button", { type: "button", class: "btn btn-big", dataset: { fkey: `qa-cancel:${slot}` } }, "Cancel");
  const confirmRow = el("div", { class: "qa-confirm" }, cancel, sendBtn);
  const armedLine = el("p", { class: "qa-armed", "aria-live": "polite" });
  const status = el("p", { class: "decision-status", role: "status", "aria-live": "polite" });
  const paint = () => {
    const done = st.status !== null || Boolean(st.sent);
    for (const b of opts) {
      b.setAttribute("aria-pressed", String(st.armed === b.dataset.key));
      b.disabled = st.busy || done;
    }
    const o = qa.options.find((x) => x.key === st.armed);
    confirmRow.hidden = !o || done;
    armedLine.textContent = o && !done ? `Answer ${q.id} with ${o.n} · ${o.label}?` : "";
    sendBtn.disabled = st.busy;
    // sent and no ack known: "applying" (paired) or "waiting", "Not applied yet" after two minutes; a waiting ack says why
    if (st.sent) {
      const ageMs = Date.now() - st.sent.at;
      st.status = outcomeText(st.sent.ack, { paired: st.sent.paired, ageMs });
      st.role = outcomeRole(st.sent.ack, { ageMs });
    }
    status.replaceChildren(st.status ? pill(st.status === QUEUED_TEXT ? "neu" : st.role || "info",
      st.role === "warn" ? "alert" : "clock", st.status) : "");
  };
  // Arming keeps the focus on the option (a screen reader hears the armed line); Cancel gives it back to the option.
  for (const b of opts) b.addEventListener("click", () => { st.armed = b.dataset.key; paint(); });
  cancel.addEventListener("click", () => {
    const was = opts.find((b) => b.dataset.key === st.armed);
    st.armed = null;
    paint();
    was?.focus();
  });
  sendBtn.addEventListener("click", async () => {
    // the same check as the ticket page's Send answer: a value the question accepts, nothing else
    if (st.busy || !st.armed || !canSendAnswer(q, st.armed)) return;
    st.busy = true;
    paint();
    try {
      const target = targetFor(doc, "answer", { qid: q.id });
      const { queued } = await sendDecisions({ row, doc, kind: "answer", ids: st.ids,
        items: [{ target, value: decisionValue("answer", q, st.armed) }] });
      const paired = Boolean(await pairingFor(null, row.space).catch(() => null));
      if (queued) st.status = QUEUED_TEXT;
      else {
        st.sent = { at: Date.now(), ack: null, paired };
        setTimeout(() => st.paint?.(), NOT_YET_MS + 500);           // then "Not applied yet" unless an ack came
      }
    } catch (e) {
      toast(e instanceof OutboxFullError ? e.message : `Couldn't send: ${e?.detail || e?.message || "error"}`, "error");
    } finally {
      st.busy = false;
      paint();
    }
  });
  st.paint = paint;                     // the newest render's elements, for the check below
  paint();
  // An answer already on its way (queued in this browser, or sent and not handled yet) is said, not offered
  // again: after a reload the card shows the receipt instead of the options.
  if (st.status === null && !st.sent && !st.checked) {
    st.checked = true;
    (async () => {
      // Only an answer to this question as the phone shows it (its qid and hash) counts: the queued item's sealed
      // body is opened with the ticket DEK, as the sent ones are.
      let queued = false;
      if (row.dek) {
        for (const x of await queuedDecisions(row.id)) {
          if (x.state === "failed" || x.decisionKind !== "answer") continue;
          try {
            const b = await openDecision(row.dek, hexToBytes(row.uuid), hexToBytes(x.uuid), x.body);
            if (b?.target?.qid === q.id && b?.target?.hash === q.hash) queued = true;
          } catch {
            /* not ours to read: not counted */
          }
        }
      }
      let sent = null;
      if (!queued && row.dek) {
        try {
          sent = (await loadDecisions(row)).find((d) => (d.ack == null || String(d.ack).startsWith("waiting-"))
            && d.body?.kind === "answer" && d.body.target?.qid === q.id && d.body.target?.hash === q.hash) || null;
        } catch {
          sent = null;
        }
      }
      if ((queued || sent) && st.status === null) {
        const paired = Boolean(await pairingFor(null, row.space).catch(() => null));
        if (queued) st.status = QUEUED_TEXT;
        else st.sent = { at: Date.parse(sent.created_at) || Date.now(), ack: sent.ack ?? null, paired };
        st.paint();
      }
    })();
  }
  // Sent and no ack seen yet: look for it again when the list re-renders (the desktop's ack wakes the long-poll),
  // at most every 10 s, so a waiting ack's reason shows up without a reload.
  if (st.sent && st.sent.ack == null && row.dek && Date.now() - (st.lastLook || 0) > 10000) {
    st.lastLook = Date.now();
    loadDecisions(row).then((list) => {
      const d = list.find((x) => x.body?.kind === "answer" && x.body.target?.qid === q.id && x.body.target?.hash === q.hash && x.ack);
      if (d) { st.sent.ack = d.ack; st.paint?.(); }
    }, () => {});
  }
  return el("div", { class: "qa", role: "group", "aria-label": q.text || q.id },
    el("p", { class: "qa-text" }, shown(q.text || q.id)),
    q.why ? el("p", { class: "dq-why" }, "Why: ", shown(q.why)) : null,
    el("div", { class: "qa-opts" }, opts), armedLine, confirmRow, status,
    el("a", { class: "qa-more", href: `/t/${row.n}#answer` }, "Other answer or a note"));
}

// What a Needs you card offers below its title: the answer in place, or the way to the ticket.
function cardAct(row, doc) {
  if (row.needs === "question") {
    if (doc.redaction === "key-only") return null;
    const qa = quickAnswer(doc);
    if (qa) return quickAnswerEl(row, doc, qa);
    const n = row.open_questions || 0;
    return el("a", { class: "btn btn-block card-go", href: `/t/${row.n}#answer` },
      el("span", {}, n > 1 ? `Answer ${n} questions` : "Answer on the ticket"), el("span", { "aria-hidden": "true" }, "›"));
  }
  const action = cardAction(row, doc);
  if (!action) return null;
  const line = cardLine(row, doc);
  // v4: the gated text's first pinned image (or the proof's for a verdict), shown only once its sha256 is verified
  const img = row.needs === "approval" ? gateImage(doc, ["requirements", "plan"]) : proofImage(doc);
  return el("div", { class: "card-act" }, line ? el("p", { class: "ncard-meta" }, line) : null,
    img ? pinnedFigure(img, "pin-card") : null,
    el("a", { class: "btn btn-primary btn-block card-go", href: `/t/${row.n}#answer` }, el("span", {}, action), el("span", { "aria-hidden": "true" }, "›")));
}

function card(row, label, { tickets = false } = {}) {
  const doc = row.doc;
  const key = doc?.id || row.id;
  const you = Boolean(row.needs);
  const lines = [];
  if (you) lines.push(NEEDS_LABEL[row.needs] || "Needs you");
  lines.push(label);
  if (row.needs === "question" && row.open_questions) lines.push(questionCount(row.open_questions));
  lines.push(shortAge(row.updated_at));
  const pills = tickets || doc?.move?.label ? [moveChipEl(row, doc)] : [doc ? needsChip(row, doc) : needsPill(row)];
  return el("article", { class: `card ncard${you ? " is-you" : ""}`, dataset: { n: String(row.n) } },
    el("a", { class: "ncard-link", href: `/t/${row.n}${you ? "#answer" : ""}` },
      el("span", { class: "ncard-row" }, el("span", { class: "ncard-pills" }, pills), el("b", { class: "ncard-key" }, key)),
      el("span", { class: "ncard-title" }, doc ? shown(cardTitle(doc, key))
        : row.rollback ? "The server sent an older copy · open it later"
          : row.error === "binding" ? "Doesn't match its sealed content · check on the desktop" : "Couldn't decrypt this ticket"),
      tickets && doc ? stripEl(doc) : null,
      el("span", { class: "ncard-meta" }, lines.filter(Boolean).join(" · "))),
    !tickets && you && doc ? cardAct(row, doc) : null);
}

function section(group, space, { tickets }) {
  const seen = lastSeenText(space?.last_seen_at);
  return el("section", { class: "ws", "aria-label": group.label },
    el("header", { class: "ws-head" },
      el("h2", { class: "ws-title" }, group.label),
      el("span", { class: "ws-sub" }, [space?.owner_name, space?.last_seen_at ? `seen ${shortAge(space.last_seen_at)}` : ""].filter(Boolean).join(" · "))),
    seen ? el("p", { class: "callout r-warn ws-seen" }, icon("clock"), el("span", {}, seen)) : null,
    group.items.map((row) => card(row, group.label, { tickets })));
}

function joinBanner(requests, spaces) {
  if (!requests.length) return null;
  const r = requests[0];
  const label = spaces.get(r.space)?.label || UNKNOWN_SPACE;
  const more = requests.length > 1 ? ` · +${requests.length - 1} more` : "";
  return el("a", { class: "callout r-you join-banner", href: "/settings#join" },
    icon("devices"), el("span", {}, `${r.device_name || "A device"} wants to sync ${label}${more}`), el("b", {}, "Review"));
}

// The ticket request card: one for the page's life, its workspace options updated in place (a new workspace, one
// removed), so the server's answer after the cached list never replaces what is being typed.
function requestCard(state) {
  if (!state.requestCard) {
    state.requestCard = ticketRequestCard({ mk: state.keys.mk, keyVersion: state.keys.keyVersion, spaces: state.spaces });
  } else {
    state.requestCard.setSpaces(state.spaces);
  }
  return state.requestCard;
}

async function render(state) {
  const { view, spaces, rows, joins, messages } = state;
  const tickets = view === "tickets";
  const list = tickets ? [...rows].filter((r) => r.doc || r.needs).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    : needsYou(rows);
  const labels = new Map([...spaces.values()].map((s) => [s.id, s.label || UNKNOWN_SPACE]));
  const groups = groupBySpace(list, labels);
  const count = document.getElementById("needs-count");
  const sub = document.getElementById("needs-sub");
  if (tickets) {
    if (count) count.textContent = String(list.length);
    if (sub) sub.textContent = groups.length ? `${groups.length} workspace${groups.length === 1 ? "" : "s"}` : "";
  } else {
    // v4: the headline counts the decisions that block an agent; the rest wait for a moment
    const n = blockingCount(list);
    const title = document.getElementById("needs-title");
    if (title) title.textContent = n ? `${n} decision${n === 1 ? "" : "s"}` : list.length ? "Nothing blocks the agents" : "Needs you";
    if (count) count.textContent = "";
    if (sub) sub.textContent = n ? "then the agents run on their own" : list.length ? `${list.length} when you have a moment` : "";
  }
  const root = document.getElementById("needs-list");
  const linked = rows.filter((r) => r.doc).length;
  const empty = tickets
    ? el("section", { class: "empty" }, el("h2", {}, "No linked tickets yet."),
      el("p", { class: "hint" }, "Link a ticket on the desktop and it shows here."))
    // Inbox zero: calm, with an honest line about what is linked.
    : el("section", { class: "card empty inbox-zero", role: "status" },
      el("span", { class: "zero-mark", "aria-hidden": "true" }, icon("check")),
      el("h2", {}, "Nothing needs you right now"),
      el("p", { class: "hint" }, `${linked ? `${linked} linked ticket${linked === 1 ? "" : "s"}, none waiting on you. ` : ""}When an agent asks a question or a plan is ready, it shows here.`));
  // Needs you: messages to the human first (spec §8). Tickets: the ticket request form (spec §5.5); it keeps
  // its own state, so it is kept across reloads while the workspaces stay the same.
  const top = tickets ? (spaces.size ? requestCard(state) : null) : messagesSection(messages, () => state.reload());
  // partial: a cached list with rows left out (older than this phone saw): never "Nothing needs you", and
  // still loading until the server answers.
  const nothing = !groups.length && !(top && !tickets) && !state.partial;
  // Three stable slots: the join banner, the top (the request card keeps its node, so a phone keyboard stays
  // open while the list refreshes) and the sections. A focused control that is rebuilt (an option of a card)
  // gets the focus back by its data-fkey.
  if (!state.slots) {
    state.slots = { join: el("div", { class: "slot-join" }), top: el("div", { class: "slot-top" }),
      search: el("div", { class: "slot-search" }), list: el("div", { class: "needs-sections" }) };
    // v4 Board: search and the lists first, the ticket request form after them
    root.replaceChildren(...(tickets ? [state.slots.join, state.slots.search, state.slots.list, state.slots.top]
      : [state.slots.join, state.slots.top, state.slots.search, state.slots.list]));
  }
  const focusKey = document.activeElement?.dataset?.fkey;
  state.slots.join.replaceChildren(joinBanner(joins, spaces) || "");
  if (state.slots.top.firstChild !== top) state.slots.top.replaceChildren(top || "");
  // the board's search box keeps its node (and the phone keyboard) while the list below re-renders
  if (tickets && list.length && !state.slots.search.firstChild) state.slots.search.replaceChildren(boardSearch(state));
  state.slots.list.replaceChildren(...(tickets ? board(state, list)
    : groups.length ? groups.map((g) => section(g, spaces.get(g.space), { tickets })) : nothing ? [empty] : []),
  !tickets ? receiptsEl(state) : "");
  if (focusKey && document.activeElement?.dataset?.fkey !== focusKey) {
    [...root.querySelectorAll("[data-fkey]")].find((n) => n.dataset.fkey === focusKey)?.focus();
  }
  root.dataset.from = state.from;
  document.getElementById("needs-loading").hidden = !state.partial;
}

// ---- v4 Board (the Tickets tab): your move, the agents with their task bar, the folded backlog; a search box filters
// as you type (title and key, on this phone only).
const BOARD_STATUS_BACKLOG = new Set(["backlog"]);

function boardRow(row) {
  const doc = row.doc;
  const key = doc?.id || row.id;
  const label = moveChip(row, doc).text;          // the document's move label (hidden characters never), else local rules
  return el("a", { class: "card brow is-you", href: `/t/${row.n}${row.needs ? "#answer" : ""}`, dataset: { n: String(row.n) } },
    el("span", { class: "pill r-you brow-dot", "aria-hidden": "true" }, icon("dot")),
    el("span", { class: "brow-text" }, el("b", {}, key), " ", shown(label)), el("span", { class: "brow-go", "aria-hidden": "true" }, "›"));
}

function agentCard(row) {
  const doc = row.doc;
  const key = doc?.id || row.id;
  const chip = taskChip(doc);
  return el("a", { class: "card bagent", href: `/t/${row.n}`, dataset: { n: String(row.n) } },
    el("span", { class: "bagent-head" }, el("b", { class: "bagent-title" }, key, " ", shown(cardTitle(doc, key))),
      chip ? el("span", { class: "pill r-info" }, icon("half"), el("span", {}, chip)) : null),
    el("span", { class: "seg5", role: "img", "aria-label": chip ? `Tasks ${chip}` : "No tasks yet" },
      taskBar(doc).map((t) => el("i", { class: `seg-${t}` }))));
}

function matches(row, q) {
  if (!q) return true;
  const doc = row.doc;
  return `${doc?.id || ""} ${row.id} ${doc?.title || ""}`.toLowerCase().includes(q);
}

function boardSearch(state) {
  const input = el("input", { class: "input board-search", type: "search", "aria-label": "Search tickets",
    placeholder: "Search — filters as you type", autocomplete: "off" });
  input.addEventListener("input", () => { state.boardQuery = input.value; render(state); });
  return input;
}

function board(state, list) {
  if (!list.length) {
    return [el("section", { class: "empty" }, el("h2", {}, "No linked tickets yet."),
      el("p", { class: "hint" }, "Link a ticket on the desktop and it shows here."))];
  }
  const q = (state.boardQuery || "").trim().toLowerCase();
  const rows = list.filter((r) => matches(r, q));
  const yours = rows.filter((r) => r.needs || r.doc?.move?.who === "you");
  const agents = rows.filter((r) => !yours.includes(r) && (r.doc?.move?.who === "agent" || r.doc?.status === "in-progress"));
  const backlog = rows.filter((r) => !yours.includes(r) && !agents.includes(r) && BOARD_STATUS_BACKLOG.has(r.doc?.status));
  const rest = rows.filter((r) => !yours.includes(r) && !agents.includes(r) && !backlog.includes(r));
  const lab = (text) => el("h2", { class: "lab" }, text);
  const fold = (title, items, cls) => (items.length ? el("details", { class: `card bfold ${cls}` },
    el("summary", {}, el("span", {}, title), el("span", { class: "muted", "aria-hidden": "true" }, "▾")),
    el("div", { class: "bfold-body" }, items.map((r) => el("a", { class: "brow-plain", href: `/t/${r.n}` },
      el("b", {}, r.doc?.id || r.id), " ", shown(cardTitle(r.doc, r.doc?.id || r.id)), idleTag(r.doc))))) : null);
  return [
    yours.length ? lab(`Your move · ${yours.length}`) : null, yours.map(boardRow),
    agents.length ? lab(`Agents · ${agents.length}`) : null, agents.map(agentCard),
    fold(`Backlog · ${backlog.length} idea${backlog.length === 1 ? "" : "s"}`, backlog, "bfold-backlog"),
    fold(`Other · ${rest.length}`, rest, "bfold-other"),
    q && !rows.length ? el("p", { class: "hint" }, "No ticket matches this search.") : null].flat().filter(Boolean);
}

// ---- v4 receipts: the phone's decisions the desktop applied in the last 24 hours, in place of their cards
const RECEIPT_VERB = { approve: "Approved", answer: "Answered", verdict: "Verdict given", request_changes: "Changes requested",
  ticket_request: "Ticket requested" };

function receiptsEl(state) {
  const items = state.receipts || [];
  if (!items.length) return "";
  return el("section", { class: "receipts", "aria-label": "Applied decisions" }, items.map((r) =>
    el("div", { class: "card receipt" }, el("span", { class: "pill r-ok" }, icon("check")),
      el("span", { class: "receipt-text" }, `${RECEIPT_VERB[r.kind] || "Decided"} ${r.mine ? "from this phone" : "from TIX"} · applied ${asOf(Date.parse(r.ack_at))}`,
        r.key ? el("span", { class: "muted" }, ` · ${r.key}`) : null))));
}

// One request per workspace, in parallel; only decisions this browser sent say "from this phone".
async function loadReceipts(spaces, rows) {
  const keys = new Map(rows.map((r) => [r.id, r.doc?.id || r.id]));
  const since = Date.now() - 86400000;
  const mine = myDecisions();
  const lists = await Promise.all([...spaces.values()].map((s) => api("GET", `/api/decisions?space=${encodeURIComponent(s.id)}`)
    .then((r) => r.decisions || [], () => [])));       // receipts are a courtesy: none when a list doesn't load
  const out = [];
  for (const d of lists.flat()) {
    const at = Date.parse(d.ack_at || "");
    if (d.ack === "applied" && at >= since) {
      out.push({ kind: d.kind, ack_at: d.ack_at, key: d.ticket ? keys.get(d.ticket) || d.ticket : "", mine: mine.has(d.id) });
    }
  }
  return out.sort((a, b) => String(b.ack_at).localeCompare(String(a.ack_at))).slice(0, 5);
}

// "14:05": when a cached list arrived, in this browser's time.
export function asOf(at) {
  const d = new Date(at);
  const two = (x) => String(x).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

async function loadJoins() {
  try {
    return (await api("GET", "/api/join-requests")).requests || [];
  } catch {
    return [];
  }
}

export async function start() {
  const view = new URLSearchParams(location.search).get("view") === "tickets" ? "tickets" : "needs";
  document.body.dataset.view = view;
  const keys = await keysOrLogin();
  if (!keys) return;
  const state = { view, keys, spaces: new Map(), rows: [], joins: [], messages: [], requestCard: null, partial: false,
    from: "" };
  // cachedAt: the list on screen is the last-known one (when it arrived); live: the server has answered.
  let cachedAt = null, live = false, skipped = 0;
  const asof = document.getElementById("needs-asof");
  const reload = async () => {
    try {
      const [spaces, rows, joins, messages] = await Promise.all([loadSpaces(keys.mk), loadMirrors(keys.mk),
        loadJoins(), view === "needs" ? loadMessages(keys.mk).catch(() => []) : []]);
      Object.assign(state, { spaces, rows, joins, messages, partial: false, from: "server" });
      live = true;
      cachedAt = null;
      document.getElementById("needs-error").hidden = true;
      if (asof) asof.hidden = true;
      skipped = 0;
      const held = document.getElementById("needs-skipped");
      if (held) held.hidden = true;
      await render(state);
      cacheLabels(state.spaces, state.rows);
      // receipts after the list is on screen: their requests never hold the first render
      if (view === "needs") {
        loadReceipts(spaces, rows).then((receipts) => {
          if (state.rows !== rows) return;          // a newer refresh took over
          state.receipts = receipts;
          render(state);
        });
      }
      window.dispatchEvent(new CustomEvent("fs:needs-changed"));
    } catch (e) {
      // The session is gone although keys are still here (the worker answered with the cached shell):
      // drop the keys and the lists, then sign in again.
      if (e?.status === 401 && !live) {
        await signInAgain();
        return;
      }
      // A last-known list on screen stays there, marked as such.
      if (cachedAt !== null && asof) {
        asof.textContent = `${e?.status === 0 ? "Offline" : "Couldn't refresh"} — as of ${asOf(cachedAt)}`;
        asof.hidden = false;
        // Rows left out of the cached list (older than this phone saw) are said, not silently missing.
        const held = document.getElementById("needs-skipped");
        if (held && skipped) {
          held.textContent = `${skipped} ticket${skipped === 1 ? "" : "s"} not shown until ${e?.status === 0 ? "you're back online" : "the server answers"}.`;
          held.hidden = false;
        }
        document.getElementById("needs-loading").hidden = true;
        return;
      }
      const err = document.getElementById("needs-error");
      err.querySelector("span").textContent = e?.status === 0 ? "You're offline. This list updates when you're back." : "Couldn't load your tickets.";
      err.hidden = false;
      document.getElementById("needs-loading").hidden = true;
    }
  };
  state.reload = reload;
  document.getElementById("needs-retry")?.addEventListener("click", reload);
  // The last-known list first (opened and checked like the server's), then the network.
  const cached = await loadCachedLists(keys.mk).catch(() => null);
  if (cached) {
    Object.assign(state, { spaces: cached.spaces, rows: cached.rows, partial: cached.skipped > 0, from: "cache" });
    cachedAt = cached.at;
    skipped = cached.skipped;
    await render(state);
  }
  await reload();
  watchMirrors(reload);
  window.addEventListener("online", reload);
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-needs")) start();
