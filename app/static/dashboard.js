const STATUS = {
  received: ["Received", "busy"], gating: ["Checking", "busy"], identifying: ["Identifying", "busy"],
  researching: ["Researching", "busy"], classifying: ["Deciding", "busy"], querying: ["Searching", "busy"],
  delivering: ["Delivering", "busy"], delivered: ["Delivered", "good"], activated: ["Activated", "great"],
  awaiting_intent: ["Asked intent", "info"], nurture: ["Self-serve", "info"], human_queue: ["Human queue", "warn"],
  internal: ["Internal", "info"], rejected: ["Rejected", "warn"], withdrawn: ["Stopped", "warn"],
  failed_safe: ["Failed safe", "warn"],
};
const STEPS = ["Gates", "Identify", "Research", "Decide", "Search", "Deliver"];
const STEP_INDEX = { received: 0, gating: 0, identifying: 1, researching: 2, classifying: 3, querying: 4, delivering: 5 };

const app = { signups: new Map(), calls: [], selected: null, tab: "email", detail: null };
const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}
const post = (path, body) =>
  api(path, { method: "POST", headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

function renderKpis(k) {
  const tiles = [
    ["Time to first result", k.avg_ttfr_s === null ? "–" : `${k.avg_ttfr_s} s`, "average, served signups"],
    ["Non-empty results", k.non_empty_rate === null ? "–" : `${k.non_empty_rate}%`, `${k.served} served`],
    ["Credits per served signup", k.credits_per_served === null ? "–" : k.credits_per_served, `${k.total_credits} total`],
    ["Activations", k.activations, "made a second call"],
    ["Sales briefs", k.briefs_sent, `${k.alerts_suppressed} alerts suppressed`],
    ["Human queue", k.abuse_catches, "held for a person"],
    ["Fallbacks", k.fallbacks, "used the basic profile"],
  ];
  $("kpis").replaceChildren(...tiles.map(([label, value, hint]) => {
    const tile = el("div", "kpi");
    tile.append(el("p", "kpi-label", label), el("p", "kpi-value", value), el("p", "kpi-hint", hint));
    return tile;
  }));
}

function statusPill(status) {
  const [label, tone] = STATUS[status] || [status, "info"];
  return el("span", `status ${tone}`, label);
}

function renderSignups() {
  const items = [...app.signups.values()].sort((a, b) => b.created_at - a.created_at);
  $("signups").replaceChildren(...items.map((s) => {
    const li = el("li");
    const button = el("button", `signup${s.id === app.selected ? " selected" : ""}`);
    button.append(el("span", "signup-email", s.email || "(empty)"), statusPill(s.status));
    const meta = el("span", "signup-meta");
    meta.textContent = [
      s.playbook && s.playbook !== "unknown" ? s.playbook : "",
      s.ttfr_ms ? `${(s.ttfr_ms / 1000).toFixed(1)} s` : "",
      `${s.credits} cr`,
    ].filter(Boolean).join(" · ");
    button.append(meta);
    if (s.fallback) button.append(el("span", "signup-fallback", "Basic profile"));
    if (s.founder) button.append(el("span", "signup-founder", "Founder, watched"));
    button.addEventListener("click", () => select(s.id));
    li.append(button);
    return li;
  }));
}

function renderHuman(items) {
  $("human-section").hidden = items.length === 0;
  $("human").replaceChildren(...items.map((item) => {
    const li = el("li", "human-item");
    li.append(el("p", "human-reason", item.reason), el("p", "human-email", item.email_raw), el("p", "human-detail", item.detail));
    li.addEventListener("click", () => select(item.signup_id));
    return li;
  }));
}

function renderLog() {
  const onlySelected = $("log-filter").checked && app.selected;
  const calls = onlySelected ? app.calls.filter((c) => c.signup_id === app.selected) : app.calls;
  $("log").replaceChildren(...calls.slice(0, 200).map(logRow));
}

function logRow(call) {
  const li = el("li", `log-row kind-${call.kind}${call.status && call.status >= 400 ? " failed" : ""}`);
  const top = el("div", "log-top");
  top.append(el("span", "log-endpoint", call.endpoint));
  if (call.status) top.append(el("span", "log-status", call.status));
  if (call.latency_ms !== null && call.latency_ms !== undefined && !call.cached) top.append(el("span", "log-latency", `${call.latency_ms} ms`));
  if (call.kind === "crustdata" || call.kind === "sandbox") {
    const label = call.credits === null || call.credits === undefined ? "n/a" : `${call.credits} cr`;
    top.append(el("span", "log-credits", label));
  }
  li.append(top);
  if (call.note) li.append(el("p", "log-note", call.note));
  return li;
}

function renderSteps(signup) {
  const reached = STEP_INDEX[signup.status];
  const finished = reached === undefined;
  const stopped = ["human_queue", "rejected", "withdrawn", "internal", "nurture", "awaiting_intent"].includes(signup.status);
  const called = new Set((signup.calls || []).map((c) => c.endpoint));
  const researched = called.has("/company/enrich");
  const searched = signup.attempts && signup.attempts.length > 0;
  $("steps").replaceChildren(...STEPS.map((name, index) => {
    let state = "todo";
    if (!finished) state = index < reached ? "done" : index === reached ? "now" : "todo";
    else if (!stopped) state = "done";
    else state = index === 0 || (index === 1 && called.has("/person/identify")) || (index === 2 && researched) || (index === 4 && searched) ? "done" : "todo";
    if (finished && !stopped && index === 2 && !researched) state = "skip";
    return el("li", `step ${state}`, name);
  }));
}

function renderDetail(signup) {
  app.detail = signup;
  $("detail-empty").hidden = true;
  $("detail").hidden = false;
  $("detail-email").textContent = signup.email_raw || "(empty)";
  $("detail-route").textContent = [signup.route, signup.route_detail].filter(Boolean).join(". ");
  $("detail-status").replaceChildren(statusPill(signup.status));
  $("detail-fallback").hidden = !signup.fallback_reason;
  $("detail-fallback").textContent = signup.fallback_reason ? `Basic profile used: ${signup.fallback_reason}.` : "";
  const decision = signup.decision;
  const confidence = decision && decision.decided_by !== "intent" ? ` (${Math.round(decision.confidence * 100)}%)` : "";
  $("detail-playbook").textContent = signup.playbook ? `${signup.playbook}${decision ? confidence : ""}` : "–";
  $("detail-time").textContent = signup.ttfr_ms ? `${(signup.ttfr_ms / 1000).toFixed(1)} s` : "–";
  const pillCredits = signup.sandbox_credits ? ` + ${signup.sandbox_credits} in sandbox` : "";
  $("detail-credits").textContent = `${signup.credits}${pillCredits}`;
  renderSteps(signup);
  renderEmailTab(signup);
  renderSandboxTab(signup);
  renderSalesTab(signup);
  renderWhyTab(signup);
  renderWatchTab(signup);
  showTab(app.tab);
}

function emptyTab(title, text) {
  const box = el("div", "empty");
  box.append(el("p", "empty-title", title), el("p", "", text));
  return box;
}

function frame(target, src, title, sandboxAttr) {
  const existing = target.querySelector("iframe");
  if (existing && existing.dataset.src === src) return;
  const iframe = el("iframe", "preview");
  iframe.src = src;
  iframe.dataset.src = src;
  iframe.title = title;
  iframe.setAttribute("sandbox", sandboxAttr);
  target.replaceChildren(iframe);
}

function renderEmailTab(signup) {
  const target = $("tab-email");
  if (!signup.has_email) {
    const running = (STATUS[signup.status] || [])[1] === "busy";
    target.replaceChildren(running
      ? emptyTab("Working on it", "The email appears here as soon as it's ready.")
      : emptyTab("No email sent", signup.route_detail || signup.route || "This signup stopped before delivery."));
    return;
  }
  const head = target.querySelector(".email-meta") || el("p", "email-meta");
  const states = { sent: "Sent", rendered: "Shown here only", failed: "Send failed" };
  const detail = signup.email_detail ? `: ${signup.email_detail}` : "";
  head.textContent = `${states[signup.email_status] || ""}${detail}. Subject: ${signup.email_subject}`;
  if (!target.querySelector(".email-meta")) target.replaceChildren(head);
  frame(emailFrameHost(target), `/api/signups/${signup.id}/email?v=${encodeURIComponent(signup.email_subject)}`, "Email preview", "allow-popups allow-popups-to-escape-sandbox");
}

function emailFrameHost(target) {
  let host = target.querySelector(".frame-host");
  if (!host) {
    host = el("div", "frame-host");
    target.append(host);
  }
  return host;
}

function renderSandboxTab(signup) {
  const target = $("tab-sandbox");
  if (!signup.sandbox_token) {
    target.replaceChildren(emptyTab("No sandbox", "Only signups who get results get a sandbox."));
    return;
  }
  frame(target, `/sandbox/${signup.sandbox_token}`, "Sandbox", "allow-scripts allow-same-origin allow-popups");
}

function renderSalesTab(signup) {
  const target = $("tab-sales");
  const brief = signup.brief;
  if (!brief) {
    const text = signup.sales_alert === "suppressed" ? signup.route : "No decision yet.";
    target.replaceChildren(emptyTab("No sales alert", text || "No alert."));
    return;
  }
  const card = el("article", "slack-card");
  const states = { sent: "Posted to Slack", rendered: "Shown here only", failed: "Slack post failed: stored here" };
  card.append(el("p", "slack-state", states[signup.slack_status] || ""), el("h3", "", `High fit: ${brief.who}`));
  card.append(el("h4", "", "Why"));
  const reasons = el("ul");
  brief.reasons.forEach((reason) => {
    const li = el("li", "", reason.claim);
    reason.citations.forEach((c) => li.append(el("span", "cite", `${c.value} (${c.citation})`)));
    reasons.append(li);
  });
  card.append(reasons);
  if (brief.angle) {
    const sources = brief.angle.citations.map((c) => c.citation).join("; ");
    card.append(el("h4", "", "How to open"), el("p", "", `${brief.angle.claim} (${sources})`));
  }
  if (brief.fallback) card.append(el("h4", "", "Basic profile only"), el("p", "", `${brief.fallback}. Check the details before acting on this.`));
  if (brief.match_note) card.append(el("h4", "", "Identity"), el("p", "", brief.match_note));
  if (brief.credits_per_month !== null && brief.credits_per_month !== undefined) {
    card.append(el("h4", "", `Estimated ${brief.credits_per_month.toLocaleString()} credits per month`));
  }
  const assumptions = el("ul", "assumptions");
  brief.assumptions.forEach((a) => assumptions.append(el("li", "", a)));
  card.append(assumptions, el("h4", "", "Freshest signal"), el("p", "", brief.freshest));
  target.replaceChildren(card);
}

function renderWhyTab(signup) {
  const target = $("tab-why");
  const parts = [];
  const decision = signup.decision;
  if (decision) {
    const section = el("section", "why-section");
    const by = { llm: "Claude", rules: "rules", "rules-fallback": "rules, after Claude failed", intent: "the signup's own choice" };
    section.append(el("h3", "", `Decided by ${by[decision.decided_by] || decision.decided_by}, for ${signup.persona_name}`));
    if (signup.person && signup.person.match_note) section.append(el("p", "cite", `This person: ${signup.person.match_note}`));
    if (decision.opening) {
      section.append(el("h3", "", "Opening"), el("p", "", decision.opening.claim));
      decision.opening.citations.forEach((c) => section.append(el("span", "cite", `${c.value} (${c.citation})`)));
      section.append(el("h3", "", "Reasons"));
    }
    const list = el("ul");
    decision.reasons.forEach((reason) => {
      const li = el("li", "", reason.claim);
      reason.citations.forEach((c) => li.append(el("span", "cite", `${c.value} (${c.citation})`)));
      list.append(li);
    });
    section.append(list);
    parts.push(section);
  }
  if (signup.profile_report) {
    const meta = signup.profile_meta || {};
    const section = el("section", "why-section");
    section.append(el("h3", "", `Profile report: ${meta.status}${meta.credits ? `, ${meta.credits} credits` : ""}`));
    if (meta.matched_on) section.append(el("p", "cite", `Matched on: ${meta.matched_on}`));
    section.append(el("p", "cite", `Tools used: ${(meta.tools || []).join(", ")}`));
    const details = el("details");
    details.append(el("summary", "", "Full report"), el("pre", "report-text", signup.profile_report));
    section.append(details);
    parts.push(section);
  }
  if (signup.snaps && signup.snaps.length) {
    const section = el("section", "why-section");
    section.append(el("h3", "", "Values snapped to canonical filters"));
    const list = el("ul");
    signup.snaps.forEach((snap) => list.append(el("li", "", `${snap.field}: "${snap.from}" became "${snap.to}"`)));
    section.append(list);
    parts.push(section);
  }
  if (signup.attempts && signup.attempts.length) {
    const section = el("section", "why-section");
    section.append(el("h3", "", "Searches sent"));
    const list = el("ol");
    signup.attempts.forEach((attempt) => {
      const outcome = attempt.error ? `failed: ${attempt.error}` : `${attempt.result_count} results, ${attempt.credits} cr`;
      list.append(el("li", "", `${attempt.label}: ${outcome}`));
    });
    section.append(list);
    parts.push(section);
  }
  if (signup.evidence && signup.evidence.length) {
    const section = el("section", "why-section");
    section.append(el("h3", "", "Evidence"));
    const table = el("table", "evidence");
    signup.evidence.forEach((item) => {
      const row = el("tr");
      row.append(el("td", "mono", item.id), el("td", "", item.value), el("td", "mono", [item.source, item.field, item.date].filter(Boolean).join(", ")));
      table.append(row);
    });
    section.append(table);
    parts.push(section);
  }
  target.replaceChildren(...(parts.length ? parts : [emptyTab("Nothing to show", "This signup stopped before any decision was needed.")]));
}

const STRENGTH = { strong: "Strong signal", weak: "Weak signal", noise: "Noise" };

async function renderWatchTab(signup) {
  const target = $("tab-watch");
  let watch;
  try {
    watch = await api(`/api/signups/${signup.id}/watch`);
  } catch {
    return; /* The next event retries. */
  }
  if (app.selected !== signup.id) return;
  if (!watch.high_value) {
    target.replaceChildren(emptyTab("Not watched", `Founder watch: ${watch.mode}. Only founders and CEOs of startups, matched with confidence, are watched.`));
    return;
  }
  const parts = [];
  const who = el("section", "why-section");
  who.append(el("h3", "", `High value: ${watch.high_value.role}`));
  watch.high_value.reasons.forEach((r) => {
    const p = el("p", "", r.claim);
    r.citations.forEach((c) => p.append(el("span", "cite", `${c.value} (${c.citation})`)));
    who.append(p);
  });
  if (watch.stopped) who.append(el("p", "cite", "They asked us to stop, so their watches are cancelled."));
  parts.push(who);

  const watches = el("section", "why-section");
  watches.append(el("h3", "", `Watches (${watch.mode})`));
  const list = el("ul");
  watch.watches.forEach((w) => {
    const li = el("li", "", `${w.dataset}: ${w.status}${w.crustdata_id ? `, Crustdata watch ${w.crustdata_id}` : ""}${w.detail ? `. ${w.detail}` : ""}`);
    li.append(el("span", "cite", w.subject));
    if (watch.can_simulate && w.status === "active") {
      const kinds = w.dataset === "company" ? [["funding", "Funding round"], ["investor", "New investor"], ["news", "In the news"]] : [["role", "New role"], ["headline", "New headline"]];
      const row = el("div", "watch-actions");
      kinds.forEach(([kind, label]) => {
        const button = el("button", "", `Simulate: ${label}`);
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await post(`/api/watches/${w.id}/simulate?kind=${kind}`); } finally { button.disabled = false; }
          renderWatchTab(app.detail);
        });
        row.append(button);
      });
      li.append(row);
    }
    list.append(li);
  });
  watches.append(list);
  const actions = el("div", "watch-actions");
  const check = el("button", "", "Check watches now");
  check.addEventListener("click", async () => {
    check.disabled = true;
    try { await post("/api/watch/poll"); } finally { check.disabled = false; }
    renderWatchTab(app.detail);
  });
  actions.append(check);
  if (watch.has_user_md) {
    const link = el("a", "", "Open user.md");
    link.href = `/api/signups/${signup.id}/user.md`;
    link.target = "_blank";
    actions.append(link);
  }
  watches.append(actions);
  parts.push(watches);

  const activity = el("section", "why-section");
  activity.append(el("h3", "", "New activity"));
  if (!watch.events.length) activity.append(el("p", "cite", "Nothing yet. Crustdata reports a change on the run after it happens."));
  watch.events.forEach((e) => {
    const box = el("div", "watch-event");
    (e.facts || []).forEach((f) => box.append(el("p", "", f.text), el("span", "cite", [f.source, f.field, f.date].filter(Boolean).join(", "))));
    const j = e.judgement;
    if (j) {
      const by = j.decided_by === "llm" ? "Claude" : "rules";
      box.append(el("p", "", `${STRENGTH[j.strength] || j.strength}, ${j.relevant ? "relevant" : "not relevant"} (${Math.round(j.confidence * 100)}%, ${by})`));
      if (j.why) box.append(el("span", "cite", j.why.claim));
    }
    box.append(el("p", "cite", e.outcome || e.status));
    if (e.email_subject) {
      const link = el("a", "", `Email: ${e.email_subject}`);
      link.href = `/api/watch/events/${encodeURIComponent(e.uid)}/email`;
      link.target = "_blank";
      box.append(link);
    }
    activity.append(box);
  });
  parts.push(activity);
  target.replaceChildren(...parts);
}

function showTab(name) {
  app.tab = name;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  ["email", "sandbox", "sales", "why", "watch"].forEach((tab) => ($(`tab-${tab}`).hidden = tab !== name));
  updateHash();
}

function updateHash() {
  if (app.selected) history.replaceState(null, "", `#${app.selected}/${app.tab}`);
}

async function refreshDetail() {
  if (!app.selected) return;
  try {
    const signup = await api(`/api/signups/${app.selected}`);
    if (signup.id === app.selected) renderDetail(signup);
  } catch {
    /* The next event retries. */
  }
}

function select(id) {
  app.selected = id;
  renderSignups();
  renderLog();
  updateHash();
  refreshDetail();
}

let kpiTimer = null;
function refreshKpisSoon() {
  clearTimeout(kpiTimer);
  kpiTimer = setTimeout(async () => {
    try {
      const { kpis, human_queue } = await api("/api/kpis");
      renderKpis(kpis);
      renderHuman(human_queue);
    } catch {
      /* The next event retries. */
    }
  }, 150);
}

let detailTimer = null;
function refreshDetailSoon() {
  clearTimeout(detailTimer);
  detailTimer = setTimeout(refreshDetail, 120);
}

function connect() {
  const source = new EventSource("/events");
  source.addEventListener("call", (event) => {
    const call = JSON.parse(event.data);
    app.calls.unshift(call);
    if (!$("log-filter").checked || call.signup_id === app.selected) $("log").prepend(logRow(call));
    if (call.signup_id === app.selected) refreshDetailSoon();
  });
  source.addEventListener("signup", (event) => {
    const signup = JSON.parse(event.data);
    app.signups.set(signup.id, signup);
    if (!app.selected) app.selected = signup.id;
    renderSignups();
    refreshKpisSoon();
    if (signup.id === app.selected) refreshDetailSoon();
  });
  source.addEventListener("reset", () => location.reload());
}

async function start() {
  const state = await api("/api/state");
  state.signups.forEach((s) => app.signups.set(s.id, s));
  app.calls = state.calls;
  renderKpis(state.kpis);
  renderHuman(state.human_queue);
  const [hashId, hashTab] = location.hash.slice(1).split("/");
  if (hashTab) app.tab = hashTab;
  const first = app.signups.has(hashId) ? hashId : state.signups.at(-1)?.id;
  renderSignups();
  renderLog();
  if (first) select(first);
  connect();
}

$("signup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("signup-email");
  const name = $("signup-name");
  const persona = $("signup-persona").value;
  const { ids } = await post("/api/signups", { email: input.value, name: name.value, persona });
  input.value = "";
  name.value = "";
  app.selected = ids[0];
  app.tab = "email";
  refreshDetailSoon();
});
$("demo-three").addEventListener("click", async () => {
  const { ids } = await post("/api/demo/three");
  app.selected = ids[0];
  refreshDetailSoon();
});
$("demo-matrix").addEventListener("click", () => post("/api/demo/matrix"));
$("reset").addEventListener("click", () => post("/api/reset"));
$("log-filter").addEventListener("change", renderLog);
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll(".drill-buttons button").forEach((button) => {
  button.addEventListener("click", async () => {
    await post("/api/faults", {
      target: button.dataset.target,
      status: Number(button.dataset.status || 500),
      times: Number(button.dataset.times || 1),
    });
    button.classList.add("armed");
    setTimeout(() => button.classList.remove("armed"), 1500);
  });
});

start();
