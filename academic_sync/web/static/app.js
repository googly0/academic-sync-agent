"use strict";
/* Semester Sync — front end. Vanilla JS, no build step.
   The server is the source of truth: every mutation returns the fresh state,
   and long work (imports, sync, OAuth) is a job that is polled to completion. */

const TOKEN = document.querySelector('meta[name="app-token"]').content;

const ICONS = {
  calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  inbox: '<path d="M3 13h5l2 3h4l2-3h5"/><path d="M5 5h14l2 8v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  plug: '<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0zM12 16v5"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m14 6 4 4"/>',
  sync: '<path d="M20 7a8 8 0 0 0-14-2L4 7"/><path d="M4 3v4h4M4 17a8 8 0 0 0 14 2l2-2"/><path d="M20 21v-4h-4"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>',
  notion: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 16V8l6 8V8"/>',
  google: '<path d="M20 12.2c0-.6 0-1.1-.1-1.6H12v3.1h4.5a3.9 3.9 0 0 1-1.7 2.5v2h2.7c1.6-1.4 2.5-3.6 2.5-6z"/><path d="M12 20c2.2 0 4.1-.7 5.5-2l-2.7-2a5 5 0 0 1-7.5-2.6H4.5v2.1A8 8 0 0 0 12 20z"/><path d="M7.3 13.4a4.8 4.8 0 0 1 0-3V8.3H4.5a8 8 0 0 0 0 7.2z"/><path d="M12 7.2c1.2 0 2.3.4 3.2 1.2l2.4-2.4A8 8 0 0 0 4.5 8.3l2.8 2.1A4.8 4.8 0 0 1 12 7.2z"/>',
  cog: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
};
const icon = (name) =>
  `<span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg></span>`;

const STAGE_LABELS = {
  extract_text: "Read",
  extract_facts: "Find deadlines",
  resolve_dates: "Resolve dates",
  validate: "Review gate",
  connect: "Google consent",
  sync_gcal: "Google Calendar",
  sync_notion: "Notion",
};
const TARGET_NAMES = { gcal: "Calendar", notion: "Notion" };
const SOURCE_ICONS = { pdf: "file", image: "image", gmail: "mail", manual: "pen" };
const REASONS = {
  contradiction_detected: {
    title: "Conflicting dates",
    explain: "The source gives two different dates. Pick the right one.",
  },
  unresolvable_date: {
    title: "Date needs you",
    explain: "The wording can't be turned into one exact day without guessing — so it wasn't.",
  },
  missing_required_fields: {
    title: "Missing details",
    explain: "A course or task name is missing.",
  },
};
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const S = {
  data: null,
  view: "upcoming",
  course: null,
  open: null,
  showPast: false,
  addMode: "screenshot",
  images: [],
  pdf: null,
  importJob: null,
  syncJob: null,
  googleJob: null,
  syncOpen: false,
  editing: null,
};

/* ------------------------------------------------------------------ utils */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function parseISO(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}
const DAY = 86400000;
const today = () => { const t = new Date(); return new Date(t.getFullYear(), t.getMonth(), t.getDate()); };
const pyWeekday = (d) => (d.getDay() + 6) % 7;
const fmt = (d, opts) => d.toLocaleDateString(undefined, opts);
const shortDate = (d) => fmt(d, { month: "short", day: "numeric" });

function week1Start() {
  const sem = S.data.semester;
  const start = parseISO(sem.start);
  return new Date(start.getTime() - ((pyWeekday(start) - sem.week_start + 7) % 7) * DAY);
}
function currentWeek() {
  if (!S.data.semester) return null;
  return Math.floor((today() - week1Start()) / DAY / 7) + 1;
}
function weekRange(n) {
  const s = new Date(week1Start().getTime() + (n - 1) * 7 * DAY);
  return [s, new Date(s.getTime() + 6 * DAY)];
}

// Well-separated hues, handed out in course order so no two courses in a
// typical semester share a colour.
const HUES = [18, 210, 145, 285, 45, 335, 185, 100, 245, 0];
function hue(course) {
  const names = courses().map(([c]) => c);
  const i = names.indexOf(course || "No course");
  return HUES[(i < 0 ? 0 : i) % HUES.length];
}

async function api(method, url, body) {
  const opts = { method, headers: { "X-App-Token": TOKEN } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3800);
}

async function pollJob(id, onUpdate, aborted = () => false) {
  for (;;) {
    if (aborted()) return null;
    let job;
    // Until the server has created the job row this 404s; keep trying.
    try { job = await api("GET", `/api/jobs/${id}`); }
    catch (e) { await new Promise((r) => setTimeout(r, 800)); continue; }
    onUpdate(job);
    if (job.status === "done" || job.status === "error") return job;
    await new Promise((r) => setTimeout(r, 600));
  }
}

const newJobId = () =>
  [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");

// Start a job and follow it. The browser chooses the id and sends it along, so
// polling begins at once — essential when the server (deployed, serverless)
// holds the request open until the work is finished instead of returning early.
async function runJob(url, body, onUpdate) {
  const id = newJobId();
  if (body instanceof FormData) body.append("job_id", id);
  else body = { ...body, job_id: id };
  let failure = null;
  const post = api("POST", url, body).catch((e) => { failure = e; });
  const job = await pollJob(id, onUpdate, () => failure !== null);
  await post;
  if (failure) throw failure;
  return job;
}

async function refresh() {
  S.data = await api("GET", "/api/state");
  render();
}

function setState(data) {
  S.data = data;
  render();
}

/* ---------------------------------------------------------------- derived */

const active = () => S.data.tasks.filter((t) => t.status === "active");
const review = () => S.data.tasks.filter((t) => t.status === "review");

function connectedTargets() {
  const c = S.data.connections;
  const out = [];
  if (c.google.calendar) out.push("gcal");
  if (c.notion.connected) out.push("notion");
  return out;
}
function needsSync(t, targets = connectedTargets()) {
  return targets.some((k) => t.sync[k].state !== "synced");
}
function courses() {
  const m = new Map();
  for (const t of S.data.tasks) {
    const name = t.course_name || "No course";
    m.set(name, (m.get(name) || 0) + 1);
  }
  return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

/* ----------------------------------------------------------------- render */

function render() {
  if (!S.data) return;
  renderSidebar();
  if (!S.data.semester && S.view !== "connections") S.view = "upcoming";
  if (location.hash.slice(1) !== S.view) history.replaceState(null, "", `#${S.view}`);
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${S.view}`));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === S.view));
  ({ upcoming: renderUpcoming, inbox: renderInbox, add: renderAdd, connections: renderConnections })[S.view]();
}

function renderSidebar() {
  const acct = $("#account");
  acct.hidden = !S.data.hosted;
  if (S.data.hosted) {
    acct.innerHTML = `<div class="acct-email" title="${esc(S.data.hosted.email)}">${esc(S.data.hosted.email)}</div>
      <button class="ghost small" data-act="sign-out" type="button">Sign out</button>`;
  }
  const sem = S.data.semester;
  $("#semester-label").textContent = sem
    ? `${sem.name}${currentWeek() > 0 ? ` · Week ${currentWeek()}` : ""}`
    : "No semester yet";
  const n = review().length;
  const badge = $("#inbox-badge");
  badge.hidden = n === 0;
  badge.textContent = n;
  $("#conn-warn").hidden = connectedTargets().length > 0;

  const list = courses();
  $("#course-list").innerHTML = list.length
    ? [["__all", S.data.tasks.length], ...list]
        .map(([name, count]) => {
          const all = name === "__all";
          const isActive = all ? !S.course : S.course === name;
          return `<button class="course-item${isActive ? " active" : ""}" data-course="${esc(name)}">
            ${all ? icon("calendar") : `<span class="swatch" style="--hue:${hue(name)}"></span>`}
            <span>${all ? "All courses" : esc(name)}</span><span class="count">${count}</span></button>`;
        })
        .join("")
    : `<div class="side-empty">Courses appear here as you add deadlines.</div>`;
}

/* ---- welcome ---- */

function welcomeHTML() {
  const start = new Date();
  return `<div class="welcome">
    <h1>Let's set up your semester.</h1>
    <p>Every "Week 5 Friday" in a syllabus, email or slide is counted from your first day of classes, so that's the one thing to set before adding deadlines.</p>
    <form class="card" id="welcome-form">
      <div class="grid two">
        <div class="field"><label for="w-name">Semester</label>
          <input class="input" id="w-name" placeholder="Fall ${start.getFullYear()}" required></div>
        <div class="field"><label for="w-start">First day of classes</label>
          <input class="input" type="date" id="w-start" required></div>
      </div>
      <div class="add-foot"><span class="spacer"></span>
        <button class="btn primary" type="submit">Start semester</button></div>
    </form>
  </div>`;
}
function bindWelcome(root) {
  const form = $("#welcome-form", root);
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      setState(await api("POST", "/api/settings", {
        semester_name: $("#w-name", root).value,
        semester_start: $("#w-start", root).value,
      }));
      S.view = "add";
      render();
      toast("Semester set. Now add your first deadlines.");
    } catch (err) { toast(err.message, true); }
  });
}

/* ---- upcoming ---- */

function renderUpcoming() {
  const root = $("#view-upcoming");
  if (!S.data.semester) { root.innerHTML = welcomeHTML(); bindWelcome(root); return; }

  const now = today();
  const cw = currentWeek();
  let tasks = active();
  if (S.course) tasks = tasks.filter((t) => (t.course_name || "No course") === S.course);
  tasks.sort((a, b) => a.exact_due_date.localeCompare(b.exact_due_date));

  const soon = active().filter((t) => {
    const d = parseISO(t.end_date || t.exact_due_date);
    return d >= now && d - now <= 7 * DAY;
  }).length;
  const targets = connectedTargets();
  const unsynced = targets.length ? active().filter((t) => needsSync(t, targets)).length : active().length;

  const sem = S.data.semester;
  const sub = cw < 1
    ? `${esc(sem.name)} starts ${fmt(parseISO(sem.start), { weekday: "long", month: "long", day: "numeric" })}`
    : `${esc(sem.name)} · Week ${cw} · ${fmt(now, { weekday: "long", month: "long", day: "numeric" })}`;

  // Group by academic week.
  const weeks = new Map();
  for (const t of tasks) {
    const w = Math.max(t.week ?? 0, 0);
    if (!weeks.has(w)) weeks.set(w, []);
    weeks.get(w).push(t);
  }
  const isPast = (w, list) => list.every((t) => parseISO(t.end_date || t.exact_due_date) < now);
  const entries = [...weeks.entries()];
  const pastCount = entries.filter(([w, l]) => isPast(w, l)).reduce((n, [, l]) => n + l.length, 0);
  const hasFuture = entries.some(([w, l]) => !isPast(w, l));
  const hidePast = hasFuture && !S.showPast;

  let body;
  if (!active().length) {
    body = `<div class="empty"><div class="big">Nothing scheduled yet</div>
      Add a syllabus, a screenshot, or scan your course emails — confirmed deadlines land here.
      <br><button class="btn primary" data-go="add">${icon("plus")}Add deadlines</button></div>`;
  } else if (!tasks.length) {
    body = `<div class="empty"><div class="big">No upcoming deadlines for ${esc(S.course)}</div></div>`;
  } else {
    body = (hidePast && pastCount
      ? `<button class="ghost show-earlier" data-act="show-past">Show ${pastCount} earlier deadline${pastCount === 1 ? "" : "s"}</button>`
      : hasFuture && pastCount && S.showPast
        ? `<button class="ghost show-earlier" data-act="hide-past">Hide earlier deadlines</button>` : "") +
      entries
        .filter(([w, l]) => !(hidePast && isPast(w, l)))
        .map(([w, list]) => {
          const past = isPast(w, list);
          let label = `Week ${w}`, range = "";
          if (w >= 1) { const [a, b] = weekRange(w); range = `${shortDate(a)} – ${shortDate(b)}`; }
          else label = "Before Week 1";
          return `<div class="week${past ? " past" : ""}">
            <div class="week-head"><span class="wk">${label}</span><span class="range">${range}</span>
              ${w === cw ? '<span class="now">This week</span>' : ""}</div>
            ${list.map(taskRow).join("")}</div>`;
        })
        .join("");
  }

  root.innerHTML = `
    <div class="page-head">
      <div><h1>${S.course ? esc(S.course) : "Upcoming"}</h1><div class="sub">${sub}</div></div>
      <div class="actions">${syncButton(unsynced)}</div>
    </div>
    <div class="stats">
      <div class="stat${soon ? " hot" : ""}"><div class="n">${soon}</div><div class="l">due in the next 7 days</div></div>
      <button class="stat${review().length ? " hot" : ""}" data-go="inbox"><div class="n">${review().length}</div><div class="l">need your review</div></button>
      <div class="stat"><div class="n">${unsynced}</div><div class="l">${targets.length ? "not synced yet" : "waiting for a connection"}</div></div>
      <div class="stat"><div class="n">${active().length}</div><div class="l">confirmed deadlines</div></div>
    </div>
    ${S.course ? `<button class="ghost" data-course="__all" style="margin:-10px 0 14px">${icon("x")}Show all courses</button>` : ""}
    ${body}`;
}

function taskRow(t) {
  const d = parseISO(t.exact_due_date);
  const open = S.open === t.id;
  const span = t.end_date ? `<span class="span-tag">through ${shortDate(parseISO(t.end_date))}</span>` : "";
  const editing = S.editing === t.id;
  return `<div class="task${open ? " open" : ""}" data-task="${t.id}" style="--hue:${hue(t.course_name)}">
    <div class="datebox"><div class="d">${d.getDate()}</div><div class="w">${fmt(d, { month: "short" })} · ${fmt(d, { weekday: "short" })}</div></div>
    <div class="bar"></div>
    <div class="t-main">
      <div class="t-title">${esc(t.task_name)}</div>
      <div class="t-meta"><span class="course-tag">${esc(t.course_name)}</span>
        ${t.grading_weight ? `<span class="weight">${esc(t.grading_weight)}</span>` : ""}${span}</div>
    </div>
    <div class="chips">${syncChips(t)}</div>
    <div class="t-detail">
      ${t.task_description ? `<div>${esc(t.task_description)}</div>` : ""}
      ${t.raw_date_expression ? `<div class="quote">“${esc(t.raw_date_expression)}”</div>` : ""}
      ${provenance(t)}
      ${editing ? editDateForm(t) : `<div class="row">
        <button class="btn" data-act="edit" data-id="${t.id}">${icon("pen")}Change date</button>
        <button class="btn danger" data-act="dismiss" data-id="${t.id}">Remove</button></div>`}
    </div>
  </div>`;
}

function editDateForm(t) {
  return `<form class="fix-row" data-form="edit" data-id="${t.id}" style="margin-top:10px">
    <div class="field"><label>Pick a date</label><input class="input" type="date" name="date" value="${esc(t.exact_due_date)}"></div>
    <div class="field"><label>Ends (optional)</label><input class="input" type="date" name="end_date" value="${esc(t.end_date || "")}"></div>
    <button class="btn primary" type="submit">Save</button>
    <button class="btn" type="button" data-act="cancel-edit">Cancel</button>
  </form>`;
}

function provenance(t) {
  if (!t.source) return "";
  const page = t.source_page && t.source.kind === "pdf" ? ` · page ${t.source_page}` : "";
  const from = t.source.kind === "gmail" && t.source.meta.from ? ` · ${esc(t.source.meta.from)}` : "";
  return `<div class="provenance">${icon(SOURCE_ICONS[t.source.kind] || "file")}${esc(t.source.label)}${page}${from}</div>`;
}

function syncChips(t) {
  return connectedTargets()
    .map((k) => {
      const s = t.sync[k];
      const label = { synced: TARGET_NAMES[k], changed: "Changed", error: "Failed", pending: TARGET_NAMES[k] }[s.state];
      const ic = k === "gcal" ? "calendar" : "notion";
      const title = {
        synced: `On ${k === "gcal" ? "Google Calendar" : "Notion"}`,
        changed: "Edited since it was synced — sync again to add the new version",
        error: s.error || "Sync failed",
        pending: "Not synced yet",
      }[s.state];
      const inner = `${icon(s.state === "synced" ? "check" : ic)}${label}`;
      return s.url && s.state === "synced"
        ? `<a class="chip synced" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(title)}">${inner}</a>`
        : `<span class="chip ${s.state}" title="${esc(title)}">${inner}</span>`;
    })
    .join("");
}

function syncButton(unsynced) {
  const targets = connectedTargets();
  if (!targets.length) {
    return `<button class="btn" data-go="connections">${icon("plug")}Connect Calendar or Notion</button>`;
  }
  const running = S.syncJob && S.syncJob.status !== "done" && S.syncJob.status !== "error";
  const pop = S.syncOpen
    ? `<div class="popover" id="sync-pop">
        <div class="label">Sync confirmed deadlines to</div>
        <div class="hint">Only tasks that passed review are sent. Re-syncing never creates duplicates.</div>
        ${targets.map((k) => `<label class="row check"><span>${icon(k === "gcal" ? "calendar" : "notion")} ${k === "gcal" ? "Google Calendar" : "Notion"}</span>
          <input type="checkbox" name="target" value="${k}" checked></label>`).join("")}
        <button class="btn primary" style="width:100%;margin-top:10px" data-act="sync-now">Sync now</button>
      </div>`
    : "";
  return `<button class="btn primary" data-act="sync-open" ${running ? "disabled" : ""}>
      ${icon("sync")}${running ? syncProgressLabel() : unsynced ? `Sync ${unsynced}` : "All synced"}</button>${pop}`;
}

function syncProgressLabel() {
  const st = S.syncJob && S.syncJob.stages;
  const runningStage = st && Object.entries(st).find(([, v]) => v.state === "running");
  return runningStage ? `Syncing ${STAGE_LABELS[runningStage[0]]}…` : "Syncing…";
}

/* ---- inbox ---- */

function renderInbox() {
  const root = $("#view-inbox");
  let items = review();
  if (S.course) items = items.filter((t) => (t.course_name || "No course") === S.course);
  const order = ["contradiction_detected", "unresolvable_date", "missing_required_fields"];
  const groups = Object.fromEntries(order.map((k) => [k, []]));
  for (const t of items) {
    const code = order.find((k) => t.review_codes.includes(k)) || "unresolvable_date";
    groups[code].push(t);
  }

  root.innerHTML = `
    <div class="page-head"><div><h1>Inbox</h1>
      <div class="sub">Anything the app couldn't confirm on its own waits here — it never guesses a date for you.</div></div></div>
    ${items.length ? order.filter((k) => groups[k].length).map((k) => `
      <div class="reason-group">
        <div class="reason-head" style="flex-wrap:wrap"><h2>${REASONS[k].title}</h2><span class="ct">${groups[k].length}</span>
          <div class="explain">${REASONS[k].explain}</div></div>
        <div class="cards">${groups[k].map(fixCard).join("")}</div>
      </div>`).join("")
    : `<div class="empty"><div class="big">Inbox zero</div>Every deadline you've added has a confirmed date.</div>`}`;
}

function reasonText(t) {
  return (t.review_reason || "").split("; ").map((part) => {
    const [code, ...rest] = part.split(": ");
    const detail = rest.join(": ");
    if (code === "unresolvable_date") {
      const m = detail.match(/^'(.*)' — (.*)$/) || detail.match(/^"(.*)" — (.*)$/);
      return m ? `“${esc(m[1])}” — ${esc(m[2])}` : esc(detail);
    }
    if (code === "missing_required_fields") return `Missing: ${esc(detail.replace(/_/g, " "))}`;
    if (code === "contradiction_detected") return "The source states conflicting dates.";
    return esc(part);
  }).join("<br>");
}

function fixCard(t) {
  const missing = t.review_codes.includes("missing_required_fields");
  const contra = t.review_codes.includes("contradiction_detected");
  const email = t.source && t.source.kind === "gmail" && t.source.meta.date
    ? `<div class="hint" style="margin-bottom:8px">Email sent ${esc(t.source.meta.date)} — use it to work out what “this Friday” meant.</div>` : "";
  return `<form class="card fix-card" data-form="fix" data-id="${t.id}" style="--hue:${hue(t.course_name)}">
    <div class="top">
      <span class="title">${esc(t.task_name || "Untitled task")}</span>
      ${t.course_name ? `<span class="course-tag">${esc(t.course_name)}</span>` : ""}
      ${t.grading_weight ? `<span class="weight">${esc(t.grading_weight)}</span>` : ""}
    </div>
    <div class="why">${reasonText(t)}</div>
    ${contra && t.contradiction_quotes.length ? `<div class="quotes">${t.contradiction_quotes
      .map((q, i) => `<div class="q"><span class="n">Source ${i + 1}</span>“${esc(q)}”</div>`).join("")}</div>` : ""}
    ${email}
    ${missing ? `<div class="grid two" style="margin-bottom:12px">
      <div class="field"><label>Course</label><input class="input" name="course_name" value="${esc(t.course_name || "")}" list="course-names"></div>
      <div class="field"><label>Task</label><input class="input" name="task_name" value="${esc(t.task_name || "")}"></div></div>` : ""}
    <div class="fix-row">
      ${contra ? "" : `<div class="field"><label>Date wording</label>
        <input class="input" name="date_phrase" value="${esc(t.raw_date_expression || "")}" placeholder="e.g. Week 5 Friday, Oct 10"></div>
        <span class="or">or</span>`}
      <div class="field"><label>${contra ? "The correct date" : "Pick a date"}</label><input class="input" type="date" name="date"></div>
      <div class="field" style="flex:0 1 160px"><label>Ends (optional)</label><input class="input" type="date" name="end_date"></div>
    </div>
    ${provenance(t)}
    <div class="fix-actions">
      <button class="btn primary" type="submit">${icon("check")}Confirm</button>
      <button class="btn danger" type="button" data-act="dismiss" data-id="${t.id}">Not a deadline</button>
    </div>
  </form>`;
}

/* ---- add ---- */

function renderAdd() {
  const root = $("#view-add");
  if (!S.data.semester) { root.innerHTML = welcomeHTML(); bindWelcome(root); return; }
  const g = S.data.connections.google;
  const modes = [
    ["screenshot", "image", "Screenshot", "Slides, course pages, chats — paste or drop"],
    ["gmail", "mail", "Course emails", "Scan Gmail for announcements"],
    ["pdf", "file", "Syllabus PDF", "The whole semester at once"],
    ["manual", "pen", "Type it", "Quick add a single deadline"],
  ];
  const backend = S.data.settings.llm_backend;

  let panel = "";
  if (S.addMode === "screenshot") {
    const ocr = S.data.connections.ocr_problem;
    panel = `<div class="card">
      ${ocr ? `<div class="error-box" style="margin:0 0 14px">Screenshots need OCR: ${esc(ocr)}. On macOS: <span class="mono">brew install tesseract</span></div>` : ""}
      <div class="drop" id="img-drop">
        <div class="big">Drop screenshots or photos here</div>
        <div class="sub">or click to choose · or just press <kbd>⌘</kbd> <kbd>V</kbd> anywhere to paste one</div>
      </div>
      <input type="file" id="img-input" accept="image/*" multiple hidden>
      ${S.images.length ? `<div class="thumbs">${S.images.map((f, i) => `<div class="thumb"><img src="${f.url}" alt="">
        <button type="button" data-act="rm-img" data-i="${i}" aria-label="Remove">×</button></div>`).join("")}</div>` : ""}
      <div class="add-foot">${backendSelect(backend)}<span class="spacer"></span>
        <button class="btn primary" data-act="import-images" ${S.images.length && !jobRunning(S.importJob) ? "" : "disabled"}>
          ${S.images.length ? `Find deadlines in ${S.images.length} image${S.images.length === 1 ? "" : "s"}` : "Find deadlines"}</button></div>
    </div>`;
  } else if (S.addMode === "gmail") {
    panel = !g.gmail
      ? `<div class="card"><h3>Connect Gmail first</h3>
          <p class="lead">Read-only access lets the app find instructor and Canvas announcements in your inbox. It can't send, delete, or label anything.</p>
          <button class="btn primary" data-go="connections">${icon("google")}Go to Connections</button></div>`
      : `<div class="card">
          <div class="field"><label for="g-query">Gmail search</label>
            <textarea class="input" id="g-query" rows="2">${esc(S.data.gmail_query)}</textarea>
            <div class="hint">Standard Gmail search syntax. Emails already scanned are skipped automatically.</div></div>
          <div class="add-foot">${backendSelect(backend)}
            <label class="inline-select">Up to <select id="g-max"><option>10</option><option selected>25</option><option>50</option></select> emails</label>
            <span class="spacer"></span>
            <button class="btn primary" data-act="import-gmail" ${jobRunning(S.importJob) ? "disabled" : ""}>${icon("mail")}Scan inbox</button></div>
        </div>`;
  } else if (S.addMode === "pdf") {
    panel = `<div class="card">
      <div class="drop" id="pdf-drop"><div class="big">Drop a syllabus PDF</div>
        <div class="sub">or click to choose · scanned PDFs are OCR'd · <button type="button" class="link" data-act="demo-pdf">use the sample syllabus</button></div></div>
      <input type="file" id="pdf-input" accept="application/pdf,.pdf" hidden>
      ${S.pdf ? `<div class="file-pill">${icon("file")}${esc(S.pdf.name)}</div>` : ""}
      <div class="add-foot">${backendSelect(backend)}<span class="spacer"></span>
        <button class="btn primary" data-act="import-pdf" ${S.pdf && !jobRunning(S.importJob) ? "" : "disabled"}>Find deadlines</button></div>
    </div>`;
  } else {
    panel = `<form class="card" data-form="manual">
      <div class="grid two">
        <div class="field"><label>Course</label><input class="input" name="course_name" list="course-names" placeholder="CS 231" required></div>
        <div class="field"><label>What's due</label><input class="input" name="task_name" placeholder="Problem Set 4" required></div>
        <div class="field"><label>When</label><input class="input" name="date_phrase" placeholder="Week 6 Friday · Oct 17 · 2026-10-17" required>
          <div class="hint">Written however you like — it's resolved the same way a syllabus is, and anything vague goes to the Inbox.</div></div>
        <div class="field"><label>Weight (optional)</label><input class="input" name="grading_weight" placeholder="10%"></div>
      </div>
      <div class="add-foot"><span class="spacer"></span><button class="btn primary" type="submit">${icon("plus")}Add deadline</button></div>
    </form>`;
  }

  const sources = S.data.sources.filter((s) => s.kind !== "manual");
  root.innerHTML = `
    <div class="page-head"><div><h1>Add deadlines</h1>
      <div class="sub">From wherever they actually show up. Everything is checked before it reaches your calendar.</div></div></div>
    <div class="sources">${modes.map(([k, ic, nm, ds]) => `<button class="source-tile${S.addMode === k ? " active" : ""}" data-mode="${k}">
      ${icon(ic)}<span class="nm">${nm}</span><span class="ds">${ds}</span></button>`).join("")}</div>
    ${panel}
    ${jobPanel(S.importJob)}
    <datalist id="course-names">${courses().map(([c]) => `<option value="${esc(c)}">`).join("")}</datalist>
    ${sources.length ? `<h2 class="section">Recent imports</h2><div class="recent">${sources.slice(0, 10).map((s) => `
      <div class="recent-item">${icon(SOURCE_ICONS[s.kind] || "file")}<span class="nm">${esc(s.label)}</span>
        <span class="ct">${s.task_count} task${s.task_count === 1 ? "" : "s"} · ${fmt(new Date(s.created_at), { month: "short", day: "numeric" })}</span></div>`).join("")}</div>` : ""}`;
  bindAddInputs(root);
}

function backendSelect(current) {
  return `<label class="inline-select">Extraction <select id="backend-pick">${S.data.backends
    .map((b) => `<option ${b === current ? "selected" : ""}>${esc(b)}</option>`).join("")}</select></label>`;
}

const jobRunning = (job) => job && job.status !== "done" && job.status !== "error";

function jobPanel(job) {
  if (!job) return "";
  const keys = Object.keys(STAGE_LABELS).filter((k) => job.stages[k]);
  const r = job.result;
  const summary = job.status === "done" && r
    ? `<div class="result-line">${r.added ? `Added ${r.added} deadline${r.added === 1 ? "" : "s"}` : "No new deadlines"}${
        r.flagged ? ` · ${r.flagged} need${r.flagged === 1 ? "s" : ""} your review` : ""}${
        r.duplicates ? ` · ${r.duplicates} already known` : ""}${
        r.new_emails !== undefined ? ` · ${r.new_emails} new email${r.new_emails === 1 ? "" : "s"} read` : ""}</div>
       <div class="add-foot" style="margin-top:10px">
         <button class="btn" data-go="upcoming">${icon("calendar")}See Upcoming</button>
         ${r.flagged ? `<button class="btn primary" data-go="inbox">${icon("inbox")}Review ${r.flagged}</button>` : ""}</div>`
    : "";
  return `<div class="card job"><h3>${esc(job.label)}</h3>
    <div class="stages">${keys.map((k) => `<div class="stage ${job.stages[k].state}"><span class="s-dot"></span>
      <span class="s-name">${STAGE_LABELS[k]}</span><span class="s-msg">${esc(job.stages[k].message)}</span></div>`).join("")}</div>
    ${job.status === "error" ? `<div class="error-box">${esc(job.error)}</div>` : ""}${summary}</div>`;
}

function bindAddInputs(root) {
  const wire = (dropSel, inputSel, accept) => {
    const drop = $(dropSel, root), input = $(inputSel, root);
    if (!drop) return;
    drop.addEventListener("click", (e) => { if (!e.target.closest("button")) input.click(); });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); accept([...e.dataTransfer.files]); });
    input.addEventListener("change", () => accept([...input.files]));
  };
  wire("#img-drop", "#img-input", addImages);
  wire("#pdf-drop", "#pdf-input", (files) => {
    const f = files.find((x) => x.name.toLowerCase().endsWith(".pdf"));
    if (!f) return toast("That isn't a PDF.", true);
    S.pdf = f; render();
  });
}

function addImages(files) {
  const imgs = files.filter((f) => f.type.startsWith("image/"));
  if (!imgs.length) return toast("Those aren't images.", true);
  for (const f of imgs) S.images.push({ file: f, url: URL.createObjectURL(f) });
  render();
}

async function startImport(url, body, label) {
  try {
    S.importJob = { label, status: "queued", stages: {} };
    render();
    const job = await runJob(url, body, (j) => { S.importJob = j; if (S.view === "add") renderAdd(); });
    if (job.status === "done") {
      if (url.includes("images")) { S.images.forEach((i) => URL.revokeObjectURL(i.url)); S.images = []; }
      if (url.includes("pdf")) S.pdf = null;
      await refresh();
    }
  } catch (err) { toast(err.message, true); }
}

/* ---- connections ---- */

function renderConnections() {
  const root = $("#view-connections");
  const c = S.data.connections, g = c.google, n = c.notion, sem = S.data.semester, st = S.data.settings;
  const googleRunning = jobRunning(S.googleJob);
  root.innerHTML = `
    <div class="page-head"><div><h1>Connections</h1>
      <div class="sub">Where confirmed deadlines go, and where new ones come from.</div></div></div>

    <div class="card conn"><div class="conn-ico">${icon("google")}</div><div>
      <h3>Google <span class="status ${g.connected ? "on" : "off"}">${g.connected ? "Connected" : "Not connected"}</span></h3>
      <p class="lead">Calendar ${g.calendar ? "✓" : "—"} &nbsp;·&nbsp; Gmail (read-only) ${g.gmail ? "✓" : "—"}</p>
      ${g.client_configured ? "" : `<ol class="steps">
        <li>In <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud Console</a>, enable the <b>Google Calendar API</b> and <b>Gmail API</b>.</li>
        <li>OAuth consent screen → External → add yourself as a test user.</li>
        <li>Credentials → Create OAuth client ID → <b>Desktop app</b> → download JSON.</li>
        <li>Save it as <code>credentials.json</code> in the project folder, then press Connect.</li></ol>`}
      ${g.error ? `<div class="error-box">${esc(g.error)}</div>` : ""}
      <div class="add-foot" style="margin-top:0">
        <button class="btn primary" data-act="connect-google" ${g.client_configured && !googleRunning ? "" : "disabled"}>
          ${icon("google")}${googleRunning ? "Waiting for Google…" : g.calendar && g.gmail ? "Reconnect" : "Connect Google"}</button>
        ${googleRunning ? `<span class="hint">Finish in the Google tab that just opened.</span>` : ""}
      </div>
      ${S.googleJob && S.googleJob.status === "error" ? `<div class="error-box">${esc(S.googleJob.error)}</div>` : ""}
      <form class="fix-row" data-form="calendar" style="margin-top:16px">
        <div class="field"><label>Calendar to sync to</label><input class="input" name="calendar_id" value="${esc(st.calendar_id)}">
          <div class="hint"><span class="mono">primary</span> is your main calendar; paste a calendar ID to use a dedicated one.</div></div>
        <button class="btn" type="submit">Save</button></form>
    </div></div>

    <div class="card conn"><div class="conn-ico">${icon("notion")}</div><div>
      <h3>Notion <span class="status ${n.connected ? "on" : "off"}">${n.connected ? "Connected" : "Not connected"}</span></h3>
      <p class="lead">Each confirmed deadline becomes a page in a Notion database, with Course, Due, and Weight properties.</p>
      <ol class="steps">
        <li>Create an internal integration at <a href="https://www.notion.so/my-integrations" target="_blank" rel="noopener">notion.so/my-integrations</a> and copy its secret.</li>
        <li>Open your database in Notion → ••• → <b>Connections</b> → add the integration.</li>
        <li>Paste the secret and the database link below. Missing properties are added for you.</li></ol>
      <form data-form="notion" class="grid two">
        <div class="field"><label>Integration secret</label><input class="input" type="password" name="token" autocomplete="off"
          placeholder="${n.token_configured ? "•••••••• (saved)" : "ntn_…"}"></div>
        <div class="field"><label>Database link or ID</label><input class="input" name="database_id" value="${esc(n.database_id || "")}" placeholder="https://www.notion.so/…" required></div>
        <div class="add-foot" style="grid-column:1/-1;margin-top:0">
          <button class="btn primary" type="submit">${icon("notion")}${n.connected ? "Save & check" : "Connect Notion"}</button>
          ${n.connected ? `<button class="btn danger" type="button" data-act="disconnect-notion">Disconnect</button>` : ""}</div>
      </form>
    </div></div>

    <h2 class="section">Semester</h2>
    <form class="card" data-form="semester">
      <div class="grid three">
        <div class="field"><label>Name</label><input class="input" name="semester_name" value="${esc(sem ? sem.name : "")}" required></div>
        <div class="field"><label>First day of classes</label><input class="input" type="date" name="semester_start" value="${esc(sem ? sem.start : "")}" required></div>
        <div class="field"><label>Weeks start on</label><select class="input" name="week_start">${WEEKDAYS
          .map((d, i) => `<option value="${i}" ${sem && sem.week_start === i ? "selected" : ""}>${d}</option>`).join("")}</select></div>
      </div>
      <label class="check" style="margin-top:14px"><input type="checkbox" name="day_first" ${sem && sem.day_first ? "checked" : ""}>
        Read numeric dates day-first (10/03 = 10 March)</label>
      <div class="hint" style="margin-top:6px">Changing these affects deadlines added from now on.</div>
      <div class="add-foot"><span class="spacer"></span><button class="btn" type="submit">Save semester</button></div>
    </form>

    <h2 class="section">Extraction</h2>
    <form class="card" data-form="extraction">
      <div class="grid two">
        <div class="field"><label>Model backend</label><select class="input" name="llm_backend">${S.data.backends
          .map((b) => `<option ${b === st.llm_backend ? "selected" : ""}>${esc(b)}</option>`).join("")}</select>
          <div class="hint"><b>anthropic</b> needs <span class="mono">ANTHROPIC_API_KEY</span> in <span class="mono">.env</span>; <b>ollama</b> runs locally; <b>stub</b> returns sample tasks for trying the app offline.</div></div>
        <div class="field"><label>Model (optional)</label><input class="input" name="llm_model" value="${esc(st.llm_model)}" placeholder="backend default"></div>
      </div>
      <div class="add-foot"><span class="spacer"></span><button class="btn" type="submit">Save</button></div>
    </form>`;
}

/* ----------------------------------------------------------------- events */

document.addEventListener("click", async (e) => {
  const go = e.target.closest("[data-go]");
  if (go) { S.view = go.dataset.go; S.syncOpen = false; render(); $("#main").focus(); window.scrollTo(0, 0); return; }

  const nav = e.target.closest(".nav-item");
  if (nav) { S.view = nav.dataset.view; S.syncOpen = false; render(); window.scrollTo(0, 0); return; }

  const course = e.target.closest("[data-course]");
  if (course) {
    S.course = course.dataset.course === "__all" ? null : course.dataset.course;
    if (S.view !== "inbox") S.view = "upcoming";
    render(); return;
  }

  const mode = e.target.closest("[data-mode]");
  if (mode) { S.addMode = mode.dataset.mode; render(); return; }

  const act = e.target.closest("[data-act]");
  if (act) { await handleAction(act.dataset.act, act, e); return; }

  if (S.syncOpen && !e.target.closest("#sync-pop")) { S.syncOpen = false; render(); return; }

  const row = e.target.closest(".task");
  if (row && !e.target.closest(".t-detail") && !e.target.closest("a")) {
    const id = Number(row.dataset.task);
    S.open = S.open === id ? null : id;
    S.editing = null;
    render();
  }
});

async function handleAction(name, el, e) {
  try {
    switch (name) {
      case "show-past": S.showPast = true; render(); break;
      case "hide-past": S.showPast = false; render(); break;
      case "edit": S.editing = Number(el.dataset.id); render(); break;
      case "cancel-edit": S.editing = null; render(); break;
      case "dismiss": {
        setState(await api("DELETE", `/api/tasks/${el.dataset.id}`));
        toast("Removed. It won't come back on re-import.");
        break;
      }
      case "sync-open": e.stopPropagation(); S.syncOpen = !S.syncOpen; render(); break;
      case "sync-now": {
        const targets = $$('#sync-pop input[name="target"]:checked').map((i) => i.value);
        if (!targets.length) return toast("Pick at least one place to sync to.", true);
        S.syncOpen = false;
        S.syncJob = { status: "running", stages: {} };
        render();
        const job = await runJob("/api/sync", { targets }, (j) => { S.syncJob = j; if (S.view === "upcoming") renderUpcoming(); });
        await refresh();
        if (job.status === "error") toast(job.error, true);
        else {
          const parts = Object.entries(job.result).map(([k, r]) =>
            r.interrupted ? `${TARGET_NAMES[k]}: stopped — ${r.error}` : `${TARGET_NAMES[k]}: ${r.created} added${r.adopted ? `, ${r.adopted} already there` : ""}`);
          const bad = Object.values(job.result).some((r) => r.interrupted);
          toast(parts.join(" · ") + (bad ? " · Sync again to resume." : ""), bad);
        }
        break;
      }
      case "rm-img": {
        const [img] = S.images.splice(Number(el.dataset.i), 1);
        if (img) URL.revokeObjectURL(img.url);
        render(); break;
      }
      case "import-images": {
        const fd = new FormData();
        S.images.forEach((i) => fd.append("files", i.file, i.file.name || "pasted.png"));
        fd.append("backend", $("#backend-pick").value);
        await startImport("/api/import/images", fd, `${S.images.length} image${S.images.length === 1 ? "" : "s"}`);
        break;
      }
      case "import-pdf": {
        const fd = new FormData();
        fd.append("file", S.pdf, S.pdf.name);
        fd.append("backend", $("#backend-pick").value);
        await startImport("/api/import/pdf", fd, S.pdf.name);
        break;
      }
      case "demo-pdf": {
        const blob = await (await fetch("/api/demo-pdf")).blob();
        S.pdf = new File([blob], "sample_syllabus_cs231.pdf", { type: "application/pdf" });
        render(); break;
      }
      case "import-gmail": {
        await startImport("/api/import/gmail", {
          query: $("#g-query").value,
          max_results: Number($("#g-max").value),
          backend: $("#backend-pick").value,
        }, "Gmail scan");
        break;
      }
      case "connect-google": {
        const res = await api("POST", "/api/connections/google", {});
        // Deployed, consent is a normal redirect through Google and back.
        if (res.redirect) { location.href = res.redirect; return; }
        const { job_id } = res;
        S.googleJob = { status: "running", stages: {} };
        render();
        const job = await pollJob(job_id, (j) => { S.googleJob = j; });
        await refresh();
        if (job.status === "done") toast("Google connected.");
        break;
      }
      case "sign-out":
        await api("POST", "/auth/logout", {});
        location.href = "/";
        break;
      case "disconnect-notion": setState(await api("DELETE", "/api/connections/notion")); toast("Notion disconnected."); break;
    }
  } catch (err) { toast(err.message, true); }
}

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-form]");
  if (!form) return;
  e.preventDefault();
  const v = Object.fromEntries(new FormData(form).entries());
  try {
    switch (form.dataset.form) {
      case "fix": {
        const id = Number(form.dataset.id);
        const before = S.data.tasks.find((t) => t.id === id);
        const changes = {};
        if ("course_name" in v) changes.course_name = v.course_name;
        if ("task_name" in v) changes.task_name = v.task_name;
        if (v.date) { changes.date = v.date; if (v.end_date) changes.end_date = v.end_date; }
        else if ("date_phrase" in v && v.date_phrase !== (before.raw_date_expression || "")) changes.date_phrase = v.date_phrase;
        if (!Object.keys(changes).length) return toast("Change the wording or pick a date first.", true);
        const state = await api("PATCH", `/api/tasks/${id}`, changes);
        const after = state.tasks.find((t) => t.id === id);
        setState(state);
        if (after && after.status === "active") toast(`“${after.task_name}” confirmed for ${shortDate(parseISO(after.exact_due_date))}.`);
        else toast("Still can't pin that down — try a specific date.", true);
        break;
      }
      case "edit": {
        const changes = { date: v.date };
        if (v.end_date && v.end_date !== v.date) changes.end_date = v.end_date;
        S.editing = null;
        setState(await api("PATCH", `/api/tasks/${form.dataset.id}`, changes));
        toast("Date updated. Sync again to put the new date on your calendar.");
        break;
      }
      case "manual": {
        const state = await api("POST", "/api/tasks", v);
        setState(state);
        const added = state.tasks[state.tasks.length - 1];
        form.reset();
        toast(added && added.status === "review" ? "Added to your Inbox — the date needs a closer look." : "Added to Upcoming.");
        break;
      }
      case "semester":
        setState(await api("POST", "/api/settings", { ...v, day_first: form.day_first.checked }));
        toast("Semester saved."); break;
      case "extraction":
        setState(await api("POST", "/api/settings", v)); toast("Saved."); break;
      case "calendar":
        setState(await api("POST", "/api/settings", v)); toast("Calendar saved."); break;
      case "notion": {
        const res = await api("POST", "/api/connections/notion", v);
        setState(res);
        toast(`Connected to “${res.database.title}”${res.database.added.length ? ` — added ${res.database.added.join(", ")}` : ""}.`);
        break;
      }
    }
  } catch (err) { toast(err.message, true); }
});

document.addEventListener("change", async (e) => {
  if (e.target.id === "backend-pick") {
    try { S.data = await api("POST", "/api/settings", { llm_backend: e.target.value }); }
    catch (err) { toast(err.message, true); }
  }
});

// Paste a screenshot from anywhere in the app.
document.addEventListener("paste", (e) => {
  if (e.target.closest && e.target.closest("input, textarea")) return;
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length || !S.data?.semester) return;
  e.preventDefault();
  S.view = "add";
  S.addMode = "screenshot";
  addImages(files.map((f, i) => new File([f], f.name && f.name !== "image.png" ? f.name : `pasted-${Date.now()}-${i}.png`, { type: f.type })));
  toast("Screenshot added — press “Find deadlines” when ready.");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && (S.syncOpen || S.editing)) { S.syncOpen = false; S.editing = null; render(); }
});

/* ------------------------------------------------------------------ theme */

function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function paintThemeButton() {
  const btn = $("#theme-toggle");
  btn.innerHTML = `${icon(currentTheme() === "dark" ? "sun" : "moon")}<span>${currentTheme() === "dark" ? "Light mode" : "Dark mode"}</span>`;
}
$("#theme-toggle").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
  paintThemeButton();
});

/* ------------------------------------------------------------------- boot */

$$("[data-icon]").forEach((el) => { el.outerHTML = icon(el.dataset.icon); });
paintThemeButton();
const VIEWS = ["upcoming", "inbox", "add", "connections"];
const fromHash = () => (VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : null);
S.view = fromHash() || S.view;
window.addEventListener("hashchange", () => {
  const v = fromHash();
  if (v && v !== S.view) { S.view = v; render(); }
});
refresh().then(() => {
  if (!fromHash() && S.data.semester && !S.data.tasks.length) { S.view = "add"; render(); }
}).catch((err) => toast(`Couldn't reach the app server: ${err.message}`, true));
