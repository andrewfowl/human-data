const KEY_STORAGE = "hdf_api_key";
const root = document.getElementById("root");

const TRACKS = ["financial_accounting", "audit_assurance", "tax", "financial_analysis", "managerial_accounting"];
const TASK_TYPES = ["sft", "preference", "eval"];
const ROLES = ["admin", "ops", "reviewer", "expert", "client"];
const CONTENT_TEMPLATES = {
  sft: { response: "", citations: [] },
  preference: { chosen: "", rejected: "", rejection_rationale: "", citations: [] },
  eval: { answer: "", grading_notes: "", citations: [] },
};

let me = null;

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  const key = sessionStorage.getItem(KEY_STORAGE);
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    if (data && typeof data === "object" && data.detail) {
      msg = Array.isArray(data.detail)
        ? data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
        : String(data.detail);
    }
    if (res.status === 401 && path !== "/me") { signOut(); }
    throw new ApiError(res.status, msg);
  }
  return data;
}

let toastTimer;
function toast(message, kind = "") {
  const t = document.getElementById("toast");
  t.textContent = message;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, 3500);
}

const label = (s) => String(s ?? "").replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
const shortId = (id) => (id ? String(id).slice(0, 8) : "—");
const fmtDate = (d) => (d ? new Date(d).toLocaleString() : "—");
const fmtDay = (d) => (d ? new Date(d).toLocaleDateString() : "—");
function money(cents, currency = "usd") {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: currency.toUpperCase() }).format((cents || 0) / 100);
  } catch { return `${((cents || 0) / 100).toFixed(2)} ${currency}`; }
}

const STATUS_TONE = {
  approved: "ok", qualified: "ok", paid: "ok", completed: "ok", pass: "ok", released: "ok",
  human_review: "warn", needs_revision: "warn", auto_qc_pending: "warn", pending: "warn", assigned: "warn",
  open: "warn", issued_external: "warn", draft: "neutral", revise: "warn", pending_approval: "warn",
  rejected: "bad", auto_check_failed: "bad", suspended: "bad", fail: "bad", void: "bad",
};
const badge = (status) => el("span", { class: `badge ${STATUS_TONE[status] || "neutral"}` }, label(status));

function idCell(id) { return el("span", { class: "id", title: id }, shortId(id)); }

function table(columns, rows, empty = "Nothing here yet.") {
  if (!rows || rows.length === 0) return el("p", { class: "empty" }, empty);
  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {}, columns.map((c) => el("th", { scope: "col", class: c.num ? "num" : "" }, c.label)))),
      el("tbody", {}, rows.map((r) => el("tr", {}, columns.map((c) => el("td", { class: [c.num && "num", c.truncate && "truncate"].filter(Boolean).join(" ") }, c.render(r))))))));
}

function card(title, ...children) {
  return el("section", { class: "card", "aria-label": typeof title === "string" ? title : undefined },
    title ? el("div", { class: "card-head" }, typeof title === "string" ? el("h2", {}, title) : title) : null,
    ...children);
}

function stat(lbl, value) {
  return el("div", { class: "stat" }, el("span", { class: "label" }, lbl), el("span", { class: "value" }, value));
}

function pageHead(title, subtitle, crumb) {
  return el("header", { class: "page-head" },
    el("div", {},
      crumb ? el("p", { class: "crumb" }, el("a", { href: crumb.href }, crumb.text), " / ") : null,
      el("h1", {}, title),
      subtitle ? el("p", {}, subtitle) : null));
}

let fieldSeq = 0;
/**
 * Builds a form from field specs. Types: text, email, number, password, date,
 * datetime-local, textarea, json, select, checkbox. onSubmit receives parsed values.
 */
function form({ fields, submitLabel = "Save", onSubmit, resetOnSuccess = true }) {
  const errorBox = el("p", { class: "error", role: "alert" });
  const inputs = {};
  const fieldNodes = fields.map((f) => {
    const id = `f${++fieldSeq}`;
    let input;
    if (f.type === "select") {
      input = el("select", { id, name: f.name, required: f.required },
        f.placeholder ? el("option", { value: "" }, f.placeholder) : null,
        (f.options || []).map((o) => {
          const opt = typeof o === "object" ? o : { value: o, label: label(o) };
          return el("option", { value: opt.value, selected: opt.value === f.value }, opt.label);
        }));
    } else if (f.type === "textarea" || f.type === "json") {
      input = el("textarea", { id, name: f.name, required: f.required, rows: f.rows || 5, spellcheck: f.type === "json" ? "false" : undefined });
      input.value = f.type === "json" && f.value !== undefined ? JSON.stringify(f.value, null, 2) : (f.value ?? "");
    } else if (f.type === "checkbox") {
      input = el("input", { id, name: f.name, type: "checkbox", checked: f.value });
    } else {
      input = el("input", { id, name: f.name, type: f.type || "text", required: f.required, min: f.min, max: f.max, step: f.step, placeholder: f.placeholder, autocomplete: f.autocomplete || "off" });
      if (f.value !== undefined) input.value = f.value;
    }
    inputs[f.name] = { input, spec: f };
    const cls = ["field", f.type === "checkbox" && "check", (f.full || f.type === "textarea" || f.type === "json") && "full"].filter(Boolean).join(" ");
    if (f.type === "checkbox") return el("div", { class: cls }, input, el("label", { for: id }, f.label));
    return el("div", { class: cls }, el("label", { for: id }, f.label), input, f.hint ? el("span", { class: "hint" }, f.hint) : null);
  });
  const button = el("button", { type: "submit" }, submitLabel);
  const node = el("form", { novalidate: false }, el("div", { class: "fields" }, fieldNodes), errorBox, el("div", { class: "actions" }, button));
  node.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.textContent = "";
    const values = {};
    try {
      for (const [name, { input, spec }] of Object.entries(inputs)) {
        if (spec.type === "checkbox") values[name] = input.checked;
        else if (spec.type === "number") values[name] = input.value === "" ? undefined : Number(input.value);
        else if (spec.type === "json") {
          const raw = input.value.trim();
          try { values[name] = raw ? JSON.parse(raw) : undefined; } catch { throw new Error(`${spec.label} is not valid JSON`); }
        } else values[name] = input.value.trim() === "" ? undefined : input.value.trim();
      }
      button.disabled = true;
      await onSubmit(values);
      if (resetOnSuccess) node.reset();
    } catch (err) {
      errorBox.textContent = err.message || String(err);
    } finally {
      button.disabled = false;
    }
  });
  return node;
}

function secretReveal(title, key, onContinue) {
  const copy = el("button", { type: "button", class: "secondary small", onclick: async () => {
    try { await navigator.clipboard.writeText(key); toast("API key copied"); } catch { toast("Copy failed — select the key manually", "bad"); }
  } }, "Copy key");
  return el("div", { class: "secret" },
    el("h3", {}, title),
    el("p", { class: "muted" }, "Store this key now. It is shown once and cannot be retrieved later."),
    el("code", {}, key),
    el("div", { class: "actions" }, copy, onContinue ? el("button", { type: "button", onclick: onContinue }, "Continue") : null));
}

function collapsible(title, content) {
  return el("details", { class: "card" }, el("summary", {}, title), content);
}

const isInternal = () => me && (me.role === "admin" || me.role === "ops");
const isAdmin = () => me && me.role === "admin";
const canReview = () => me && ["admin", "ops", "reviewer"].includes(me.role);

// ------------------------------------------------------------------ auth

function signOut() {
  sessionStorage.removeItem(KEY_STORAGE);
  me = null;
  location.hash = "";
  renderSignIn();
}

async function signIn(key) {
  sessionStorage.setItem(KEY_STORAGE, key);
  try {
    me = await api("GET", "/me");
  } catch (err) {
    sessionStorage.removeItem(KEY_STORAGE);
    throw err.status === 401 ? new Error("That API key is invalid or has been revoked.") : err;
  }
  if (!location.hash || location.hash === "#/") location.hash = defaultRoute();
  route();
}

async function renderSignIn() {
  root.replaceChildren(el("main", { id: "main", class: "auth" }, el("div", { class: "auth-inner" }, el("p", { class: "loading" }, "Loading…"))));
  let status = { bootstrapped: true };
  try { status = await api("GET", "/bootstrap/status"); } catch { /* fall through to sign-in */ }

  const inner = el("div", { class: "auth-inner" },
    el("header", {},
      el("h1", { class: "auth-title" },
        el("img", { src: "/app/logo.svg", alt: "", width: "40", height: "40", class: "logo" }),
        "Human Data Factory"),
      el("p", {}, "Sign in with the API key issued to your account.")));

  inner.append(card("Sign in", form({
    submitLabel: "Sign in",
    resetOnSuccess: false,
    fields: [{ name: "key", label: "API key", type: "password", required: true, placeholder: "hdf_…", autocomplete: "current-password" }],
    onSubmit: ({ key }) => signIn(key),
  })));

  if (!status.bootstrapped) {
    const slot = el("div", {});
    slot.append(form({
      submitLabel: "Create admin",
      fields: [
        { name: "name", label: "Your name", required: true },
        { name: "email", label: "Email", type: "email", required: true },
      ],
      onSubmit: async (v) => {
        const res = await api("POST", "/bootstrap", v);
        slot.replaceChildren(secretReveal("Admin account created", res.api_key, () => signIn(res.api_key)));
      },
    }));
    inner.append(card("First-time setup", el("p", { class: "muted" }, "No users exist yet. Create the first admin account to get started."), slot));
  }

  inner.append(el("p", { class: "muted" }, el("a", { href: "/docs" }, "API documentation"), " · ", el("a", { href: "/" }, "About")));
  root.replaceChildren(el("main", { id: "main", class: "auth" }, inner));
}

// ------------------------------------------------------------------ navigation

function navItems() {
  const items = [];
  if (isInternal()) items.push(["#/overview", "Overview"], ["#/firms", "Firms"], ["#/experts", "Experts"], ["#/projects", "Projects"]);
  if (me.role === "client") items.push([`#/firms/${me.firm_id}`, "My firm"], ["#/projects", "Projects"]);
  if (canReview()) items.push(["#/review", "Review queue"]);
  if (["admin", "ops", "expert"].includes(me.role)) items.push(["#/my-tasks", "My tasks"]);
  if (isAdmin()) items.push(["#/users", "Users"]);
  return items;
}

function defaultRoute() {
  if (isInternal()) return "#/overview";
  if (me.role === "client") return `#/firms/${me.firm_id}`;
  if (me.role === "reviewer") return "#/review";
  return "#/my-tasks";
}

function layout(content) {
  const current = location.hash.split("/").slice(0, 2).join("/");
  const nav = el("nav", { class: "nav", "aria-label": "Primary" },
    navItems().map(([href, text]) => el("a", { href, "aria-current": href.split("/").slice(0, 2).join("/") === current ? "page" : undefined }, text)));
  const sidebar = el("aside", { class: "sidebar" },
    el("div", { class: "brand" },
      el("img", { src: "/app/logo.svg", alt: "", width: "32", height: "32", class: "logo" }),
      el("div", { class: "brand-text" }, el("strong", {}, "Human Data Factory"), el("span", {}, "Finance & accounting"))),
    nav,
    el("div", { class: "whoami" },
      el("div", {}, el("div", {}, me.name), el("div", { class: "muted" }, label(me.role))),
      el("button", { type: "button", class: "secondary small", onclick: signOut }, "Sign out")));
  const main = el("main", { id: "main", tabindex: "-1" }, content);
  root.replaceChildren(sidebar, main);
}

const routes = [
  [/^#\/overview$/, viewOverview],
  [/^#\/firms$/, viewFirms],
  [/^#\/firms\/([^/]+)$/, viewFirm],
  [/^#\/experts$/, viewExperts],
  [/^#\/projects$/, viewProjects],
  [/^#\/projects\/([^/]+)$/, viewProject],
  [/^#\/review$/, viewReviewQueue],
  [/^#\/review\/([^/]+)$/, viewSubmission],
  [/^#\/my-tasks$/, viewMyTasks],
  [/^#\/my-tasks\/([^/]+)$/, viewMyTask],
  [/^#\/users$/, viewUsers],
];

let routeSeq = 0;
async function route() {
  if (!me) return renderSignIn();
  const hash = location.hash || defaultRoute();
  const match = routes.map(([re, fn]) => [hash.match(re), fn]).find(([m]) => m);
  const seq = ++routeSeq;
  layout(el("p", { class: "loading", role: "status" }, "Loading…"));
  let content;
  try {
    content = match ? await match[1](...match[0].slice(1)) : el("p", {}, "Page not found.");
  } catch (err) {
    content = card("Something went wrong", el("p", { class: "error" }, err.message));
  }
  if (seq !== routeSeq) return;
  layout(content);
}

const refresh = () => route();

// ------------------------------------------------------------------ views: overview

async function viewOverview() {
  const [m, auditState, recon] = await Promise.all([
    api("GET", "/metrics"),
    api("GET", "/audit/verify"),
    api("GET", "/billing/reconciliation"),
  ]);
  const t = recon.totals;
  return [
    pageHead("Overview", "Pipeline health, review backlog and billing position across all firms."),
    el("div", { class: "stats" },
      stat("Submissions", m.total_submissions),
      stat("Approval rate", m.approval_rate === null ? "—" : `${Math.round(m.approval_rate * 100)}%`),
      stat("Human review backlog", m.human_review_backlog),
      stat("Uninvoiced usage", money(m.uninvoiced_usage_cents))),
    el("div", { class: "row" },
      card("Submissions by status", table(
        [{ label: "Status", render: (r) => badge(r[0]) }, { label: "Count", num: true, render: (r) => r[1] }],
        Object.entries(m.submissions_by_status), "No submissions yet.")),
      card("Experts by status", table(
        [{ label: "Status", render: (r) => badge(r[0]) }, { label: "Count", num: true, render: (r) => r[1] }],
        Object.entries(m.experts_by_status), "No experts yet."))),
    el("div", { class: "row" },
      card("Controls",
        el("p", {}, "Audit chain: ", auditState.intact ? badge("approved") : badge("rejected"), ` ${auditState.events} events`),
        el("p", {}, "Billing reconciliation: ", recon.all_clean ? el("span", { class: "badge ok" }, "Clean") : el("span", { class: "badge bad" }, "Exceptions")),
        el("p", { class: "muted" }, `QC model: ${m.qc_model}`)),
      card("Billing totals", table(
        [{ label: "Measure", render: (r) => r[0] }, { label: "Amount", num: true, render: (r) => money(r[1]) }],
        [["Metered", t.metered_cents], ["Metered, not invoiced", t.metered_uninvoiced_cents], ["Invoiced", t.invoiced_cents], ["Paid", t.paid_cents], ["Outstanding", t.outstanding_cents]]))),
  ];
}

// ------------------------------------------------------------------ views: firms

async function viewFirms() {
  const firms = await api("GET", "/firms");
  const out = [
    pageHead("Firms", "Client organizations that buy datasets and are billed for usage."),
    card(`${firms.length} firm${firms.length === 1 ? "" : "s"}`, table([
      { label: "Name", render: (f) => el("a", { href: `#/firms/${f.id}` }, f.name) },
      { label: "Billing email", render: (f) => f.billing_email },
      { label: "Mode", render: (f) => label(f.billing_mode) },
      { label: "Currency", render: (f) => f.currency.toUpperCase() },
      { label: "Reference", render: (f) => f.external_reference || "—" },
    ], firms, "No firms yet.")),
  ];
  if (isAdmin()) {
    out.push(collapsible("Add a firm", form({
      submitLabel: "Create firm",
      fields: [
        { name: "name", label: "Firm name", required: true },
        { name: "billing_email", label: "Billing email", type: "email", required: true },
        { name: "billing_mode", label: "Billing mode", type: "select", options: ["external", "stripe"], value: "external" },
        { name: "currency", label: "Currency", value: "usd" },
        { name: "external_reference", label: "PO / AP reference", hint: "Optional" },
      ],
      onSubmit: async (v) => { await api("POST", "/firms", v); toast("Firm created"); refresh(); },
    })));
  }
  return out;
}

async function viewFirm(firmId) {
  const [firm, summary, invoices, usage, recon] = await Promise.all([
    api("GET", `/firms/${firmId}`),
    api("GET", `/firms/${firmId}/billing`),
    api("GET", `/firms/${firmId}/invoices`),
    api("GET", `/firms/${firmId}/usage`),
    api("GET", `/firms/${firmId}/reconciliation`),
  ]);
  const cur = firm.currency;

  const invoiceActions = (inv) => {
    const actions = [];
    if (inv.hosted_invoice_url) actions.push(el("a", { href: inv.hosted_invoice_url, target: "_blank", rel: "noopener noreferrer" }, "View"));
    if (inv.mode === "stripe" && inv.status === "open") {
      actions.push(el("button", { type: "button", class: "small", onclick: async (e) => {
        e.target.disabled = true;
        try {
          const here = location.href;
          const res = await api("POST", `/invoices/${inv.id}/checkout`, { success_url: here, cancel_url: here });
          window.open(res.checkout_url, "_blank", "noopener");
        } catch (err) { toast(err.message, "bad"); } finally { e.target.disabled = false; }
      } }, "Pay"));
    }
    if (isInternal() && inv.mode === "external" && inv.status === "issued_external") {
      actions.push(el("button", { type: "button", class: "small secondary", onclick: async () => {
        const reference = prompt("Payment reference (e.g. wire or check number)");
        if (!reference) return;
        try { await api("POST", `/invoices/${inv.id}/external-payment`, { reference }); toast("Payment recorded"); refresh(); }
        catch (err) { toast(err.message, "bad"); }
      } }, "Record payment"));
    }
    return actions.length ? el("div", { class: "actions" }, actions) : "—";
  };

  const out = [
    pageHead(firm.name, `${label(firm.billing_mode)} billing · ${firm.billing_email}`, isInternal() ? { href: "#/firms", text: "Firms" } : null),
    el("div", { class: "stats" },
      stat("Uninvoiced usage", money(summary.uninvoiced.amount_cents, cur)),
      stat("Outstanding", money(summary.outstanding_cents, cur)),
      stat("Invoices", invoices.length),
      stat("Reconciliation", recon.clean ? "Clean" : "Exceptions")),
    card("Invoices", table([
      { label: "Number", render: (i) => i.number || idCell(i.id) },
      { label: "Status", render: (i) => badge(i.status) },
      { label: "Period", render: (i) => `${fmtDay(i.period_start)} – ${fmtDay(i.period_end)}` },
      { label: "Amount", num: true, render: (i) => money(i.subtotal_cents, i.currency) },
      { label: "Paid", render: (i) => fmtDay(i.paid_at) },
      { label: "Actions", render: invoiceActions },
    ], invoices, "No invoices yet.")),
    card("Usage", table([
      { label: "Date", render: (u) => fmtDate(u.created_at) },
      { label: "Kind", render: (u) => label(u.kind) },
      { label: "Task", render: (u) => idCell(u.task_id) },
      { label: "Qty", num: true, render: (u) => u.quantity },
      { label: "Unit", num: true, render: (u) => money(u.unit_price_cents, u.currency) },
      { label: "Amount", num: true, render: (u) => money(u.amount_cents, u.currency) },
      { label: "Invoiced", render: (u) => (u.invoice_id ? "Yes" : "No") },
    ], usage, "No metered usage yet.")),
  ];

  if (isInternal()) {
    const today = new Date().toISOString().slice(0, 10);
    const monthStart = `${today.slice(0, 8)}01`;
    out.push(collapsible("Generate invoice", form({
      submitLabel: "Generate invoice",
      fields: [
        { name: "period_start", label: "Period start", type: "date", required: true, value: monthStart },
        { name: "period_end", label: "Period end", type: "date", required: true, value: today },
      ],
      onSubmit: async (v) => {
        const end = new Date(`${v.period_end}T23:59:59Z`).toISOString();
        await api("POST", `/firms/${firmId}/invoices`, { period_start: new Date(`${v.period_start}T00:00:00Z`).toISOString(), period_end: end });
        toast("Invoice generated");
        refresh();
      },
    })));
  }
  if (isAdmin()) {
    out.push(collapsible("Set a rate card", form({
      submitLabel: "Save rate",
      fields: [
        { name: "task_type", label: "Task type", type: "select", options: TASK_TYPES, required: true },
        { name: "unit_price", label: `Unit price (${cur.toUpperCase()})`, type: "number", min: 0.01, step: 0.01, required: true, hint: "Replaces the active rate for this task type" },
      ],
      onSubmit: async (v) => {
        await api("POST", `/firms/${firmId}/rate-cards`, { task_type: v.task_type, unit_price_cents: Math.round(v.unit_price * 100) });
        toast("Rate card saved");
      },
    })));
  }
  return out;
}

// ------------------------------------------------------------------ views: experts

async function viewExperts() {
  const experts = await api("GET", "/experts");
  const expertOptions = experts.map((e) => ({ value: e.id, label: `${e.name} (${e.email})` }));
  return [
    pageHead("Experts", "Vetted contributors and reviewers. Experts must pass a track exam (85+) to be assigned work."),
    card(`${experts.length} expert${experts.length === 1 ? "" : "s"}`, table([
      { label: "Name", render: (e) => el("div", {}, el("div", {}, e.name), el("div", { class: "muted" }, e.email)) },
      { label: "Status", render: (e) => badge(e.status) },
      { label: "Credentials", render: (e) => (e.credentials || []).join(", ") || "—" },
      { label: "Reviewer", render: (e) => (e.is_reviewer ? "Yes" : "No") },
      { label: "Quality", num: true, render: (e) => (e.quality_score ?? 0).toFixed(2) },
      { label: "Approved", num: true, render: (e) => e.approved_count },
      { label: "Gold pass / fail", num: true, render: (e) => `${e.gold_pass_count} / ${e.gold_fail_count}` },
      { label: "Review sampling", num: true, render: (e) => `${Math.round(e.human_review_sampling_rate * 100)}%` },
      { label: "ID", render: (e) => idCell(e.id) },
    ], experts, "No experts yet.")),
    el("div", { class: "row" },
      card("Add an expert", form({
        submitLabel: "Create expert",
        fields: [
          { name: "name", label: "Name", required: true },
          { name: "email", label: "Email", type: "email", required: true },
          { name: "credentials", label: "Credentials", hint: "Comma separated, e.g. CPA, CFA" },
          { name: "is_reviewer", label: "Can act as reviewer", type: "checkbox" },
        ],
        onSubmit: async (v) => {
          const credentials = (v.credentials || "").split(",").map((s) => s.trim()).filter(Boolean);
          await api("POST", "/experts", { name: v.name, email: v.email, credentials, is_reviewer: v.is_reviewer });
          toast("Expert created");
          refresh();
        },
      })),
      card("Record an exam", experts.length ? form({
        submitLabel: "Record exam",
        fields: [
          { name: "expert_id", label: "Expert", type: "select", options: expertOptions, required: true, placeholder: "Select an expert" },
          { name: "track", label: "Track", type: "select", options: TRACKS, required: true },
          { name: "exam_score", label: "Score (0–100)", type: "number", min: 0, max: 100, step: 0.1, required: true },
        ],
        onSubmit: async (v) => {
          const res = await api("POST", `/experts/${v.expert_id}/exams`, { track: v.track, exam_score: v.exam_score });
          toast(res.passed ? "Passed — expert qualified for this track" : "Recorded — below pass mark");
          refresh();
        },
      }) : el("p", { class: "empty" }, "Add an expert first."))),
  ];
}

// ------------------------------------------------------------------ views: projects

async function viewProjects() {
  const [projects, firms] = await Promise.all([
    api("GET", "/projects"),
    isInternal() ? api("GET", "/firms") : Promise.resolve([]),
  ]);
  const firmName = Object.fromEntries(firms.map((f) => [f.id, f.name]));
  const out = [
    pageHead("Projects", "Dataset projects. Each project belongs to a firm and produces one task type in one track."),
    card(`${projects.length} project${projects.length === 1 ? "" : "s"}`, table([
      { label: "Name", render: (p) => el("a", { href: `#/projects/${p.id}` }, p.name) },
      ...(isInternal() ? [{ label: "Firm", render: (p) => firmName[p.firm_id] || idCell(p.firm_id) }] : []),
      { label: "Track", render: (p) => label(p.track) },
      { label: "Task type", render: (p) => p.task_type.toUpperCase() },
    ], projects, "No projects yet.")),
  ];
  if (isInternal()) {
    out.push(collapsible("Create a project", firms.length ? form({
      submitLabel: "Create project",
      fields: [
        { name: "name", label: "Project name", required: true },
        { name: "firm_id", label: "Firm", type: "select", required: true, placeholder: "Select a firm", options: firms.map((f) => ({ value: f.id, label: f.name })) },
        { name: "track", label: "Track", type: "select", options: TRACKS, required: true },
        { name: "task_type", label: "Task type", type: "select", options: TASK_TYPES, required: true },
        { name: "guidelines", label: "Guidelines for experts", type: "textarea" },
      ],
      onSubmit: async (v) => { await api("POST", "/projects", v); toast("Project created"); refresh(); },
    }) : el("p", { class: "empty" }, "Create a firm first.")));
  }
  return out;
}

async function viewProject(projectId) {
  const projects = await api("GET", "/projects");
  const project = projects.find((p) => p.id === projectId);
  if (!project) throw new Error("Project not found");
  const [tasks, report, experts] = await Promise.all([
    api("GET", `/projects/${projectId}/tasks`),
    api("GET", `/projects/${projectId}/control-report`),
    isInternal() ? api("GET", "/experts") : Promise.resolve([]),
  ]);
  const expertName = Object.fromEntries(experts.map((e) => [e.id, e.name]));
  const qualified = experts.filter((e) => e.status === "qualified");

  const assignCell = (t) => {
    if (!isInternal()) return t.assigned_to ? idCell(t.assigned_to) : "—";
    if (t.status === "completed") return expertName[t.assigned_to] || "—";
    const select = el("select", { "aria-label": `Assign task ${shortId(t.id)}` },
      el("option", { value: "" }, t.assigned_to ? expertName[t.assigned_to] || shortId(t.assigned_to) : "Unassigned"),
      qualified.filter((e) => e.id !== t.assigned_to).map((e) => el("option", { value: e.id }, e.name)));
    select.addEventListener("change", async () => {
      if (!select.value) return;
      try { await api("POST", `/tasks/${t.id}/assign/${select.value}`); toast("Task assigned"); refresh(); }
      catch (err) { toast(err.message, "bad"); select.value = ""; }
    });
    return select;
  };

  const out = [
    pageHead(project.name, `${label(project.track)} · ${project.task_type.toUpperCase()}`, { href: "#/projects", text: "Projects" }),
    el("div", { class: "stats" },
      stat("Tasks", report.total_tasks),
      stat("Approved records", report.approved_records),
      stat("Awaiting approval", report.tasks_without_approved_submission.length),
      stat("Release check", report.clean ? "Clean" : "Not ready")),
    card("Tasks", table([
      { label: "Prompt", truncate: true, render: (t) => el("span", { title: t.prompt }, t.prompt) },
      { label: "Status", render: (t) => badge(t.status) },
      ...(isInternal() ? [{ label: "Gold", render: (t) => (t.is_gold ? "Yes" : "") }] : []),
      { label: "Assigned to", render: assignCell },
      { label: "ID", render: (t) => idCell(t.id) },
    ], tasks, "No tasks yet.")),
    card("Control report",
      el("p", {}, "Audit chain: ", report.audit_chain_intact ? badge("approved") : badge("rejected"), ` ${report.audit_chain_length} events`),
      el("p", {}, `Approved without review evidence: ${report.approved_without_review_evidence.length}`)),
  ];

  if (isInternal()) {
    const exportSlot = el("div", { class: "actions" });
    const renderExportStart = () => exportSlot.replaceChildren(el("button", { type: "button", onclick: async (e) => {
      e.target.disabled = true;
      try {
        const res = await api("POST", `/projects/${projectId}/exports`);
        exportSlot.replaceChildren(
          el("p", {}, "Export batch ", idCell(res.batch_id), " requested — ", badge(res.status)),
          el("p", { class: "muted" }, "Dual control: a different internal user must approve the release."),
          el("button", { type: "button", class: "secondary", onclick: () => approveExport(res.batch_id) }, "Approve as me"));
      } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
    } }, "Request export"));
    renderExportStart();
    const approveForm = form({
      submitLabel: "Approve batch",
      fields: [{ name: "batch_id", label: "Batch ID", required: true, hint: "Requested by another internal user" }],
      onSubmit: async (v) => approveExport(v.batch_id),
    });
    out.push(el("div", { class: "row" },
      card("Add a task", form({
        submitLabel: "Create task",
        fields: [
          { name: "prompt", label: "Prompt", type: "textarea", required: true },
          { name: "context", label: "Context (JSON)", type: "json", value: {}, hint: "Source documents, figures, constraints" },
          { name: "is_gold", label: "Gold (honeypot) task", type: "checkbox" },
          { name: "gold_answer", label: "Gold answer (JSON)", type: "json", hint: "Required for gold tasks. Never shown to experts." },
        ],
        onSubmit: async (v) => {
          await api("POST", `/projects/${projectId}/tasks`, { prompt: v.prompt, context: v.context || {}, is_gold: v.is_gold, gold_answer: v.gold_answer ?? null });
          toast("Task created");
          refresh();
        },
      })),
      card("Release", el("p", { class: "muted" }, "Exports require a clean control report and approval by a second person."), exportSlot, approveForm)));
  }
  return out;
}

async function approveExport(batchId) {
  try {
    const res = await api("POST", `/exports/${batchId}/approve`, { approved_by: me.id });
    toast(`Export ${res.status}`);
    refresh();
  } catch (err) { toast(err.message, "bad"); throw err; }
}

// ------------------------------------------------------------------ views: review

async function viewReviewQueue() {
  const queue = await api("GET", "/review-queue");
  return [
    pageHead("Review queue", "Submissions sampled or escalated for human review. Author identity is withheld."),
    card(`${queue.length} awaiting review`, table([
      { label: "Submission", render: (s) => el("a", { href: `#/review/${s.submission_id}` }, shortId(s.submission_id)) },
      { label: "Task", render: (s) => idCell(s.task_id) },
      { label: "Reason", render: (s) => (s.sampled ? "Random sample" : "Escalated by QC") },
      { label: "Submitted", render: (s) => fmtDate(s.created_at) },
      { label: "", render: (s) => el("a", { class: "button small", href: `#/review/${s.submission_id}` }, "Review") },
    ], queue, "The queue is empty.")),
  ];
}

async function viewSubmission(submissionId) {
  const sub = await api("GET", `/submissions/${submissionId}`);
  const [task, experts] = await Promise.all([
    api("GET", `/tasks/${sub.task_id}`),
    isInternal() ? api("GET", "/experts") : Promise.resolve([]),
  ]);
  const reviewers = experts.filter((e) => e.is_reviewer && e.status === "qualified" && e.id !== sub.expert_id);

  const reviewerField = me.role === "reviewer"
    ? []
    : [{ name: "reviewer_id", label: "Review as", type: "select", required: true, placeholder: "Select a reviewer", options: reviewers.map((e) => ({ value: e.id, label: e.name })) }];

  const out = [
    pageHead(`Submission ${shortId(sub.id)}`, `Version ${sub.version}`, { href: "#/review", text: "Review queue" }),
    el("p", {}, "Status: ", badge(sub.status)),
    el("div", { class: "row" },
      card("Task prompt", el("p", {}, task.prompt), Object.keys(task.context || {}).length ? el("pre", {}, JSON.stringify(task.context, null, 2)) : null),
      card("Automated reviews", sub.reviews.length ? sub.reviews.map((r) => el("div", { class: "card" },
        el("div", { class: "card-head" }, el("h3", {}, label(r.kind)), el("div", { class: "actions" }, badge(r.verdict), r.overall_score != null ? el("span", { class: "muted" }, `Score ${Number(r.overall_score).toFixed(2)}`) : null)),
        r.detail ? el("details", {}, el("summary", {}, "Detail"), el("pre", {}, JSON.stringify(r.detail, null, 2))) : null)) : el("p", { class: "empty" }, "No reviews yet."))),
  ];

  if (sub.status === "human_review") {
    out.push(card("Your review", (me.role === "reviewer" && !me.expert_id) || (me.role !== "reviewer" && reviewers.length === 0)
      ? el("p", { class: "empty" }, "No qualified reviewer is available to review this submission.")
      : form({
        submitLabel: "Submit review",
        fields: [
          ...reviewerField,
          { name: "verdict", label: "Verdict", type: "select", required: true, options: [{ value: "pass", label: "Pass" }, { value: "revise", label: "Needs revision" }, { value: "fail", label: "Fail" }] },
          { name: "comments", label: "Comments", type: "textarea" },
          { name: "criterion_scores", label: "Criterion scores (JSON, optional)", type: "json", hint: 'e.g. [{"criterion": "accuracy", "score": 4}]' },
        ],
        onSubmit: async (v) => {
          const res = await api("POST", `/submissions/${submissionId}/human-review`, {
            reviewer_id: v.reviewer_id || me.expert_id,
            verdict: v.verdict,
            comments: v.comments || "",
            criterion_scores: v.criterion_scores ?? null,
          });
          toast(`Review recorded — submission ${label(res.submission_status).toLowerCase()}`);
          location.hash = "#/review";
        },
      })));
  }
  return out;
}

// ------------------------------------------------------------------ views: expert work

async function viewMyTasks() {
  const tasks = await api("GET", "/my/tasks");
  return [
    pageHead("My tasks", me.expert_id ? "Tasks assigned to you. Open a task to write and submit your answer." : "Your account is not linked to an expert record, so no tasks can be assigned to you."),
    card(`${tasks.length} assigned`, table([
      { label: "Prompt", truncate: true, render: (t) => el("a", { href: `#/my-tasks/${t.id}`, title: t.prompt }, t.prompt) },
      { label: "Project", render: (t) => t.project_name },
      { label: "Type", render: (t) => t.task_type.toUpperCase() },
      { label: "Status", render: (t) => badge(t.status) },
    ], tasks, "Nothing assigned to you right now.")),
  ];
}

async function viewMyTask(taskId) {
  const tasks = await api("GET", "/my/tasks");
  const task = tasks.find((t) => t.id === taskId);
  if (!task) throw new Error("Task not found or not assigned to you");
  const result = el("div", {});
  return [
    pageHead("Task", `${task.project_name} · ${label(task.track)} · ${task.task_type.toUpperCase()}`, { href: "#/my-tasks", text: "My tasks" }),
    el("div", { class: "row" },
      card("Prompt", el("p", {}, task.prompt), Object.keys(task.context || {}).length ? el("pre", {}, JSON.stringify(task.context, null, 2)) : null),
      task.guidelines ? card("Guidelines", el("p", {}, task.guidelines)) : null),
    card("Your submission",
      el("p", { class: "muted" }, `Fill in every field. Main text must be at least 200 characters; cite sources in "citations".`),
      form({
        submitLabel: "Submit",
        resetOnSuccess: false,
        fields: [{ name: "content", label: "Content (JSON)", type: "json", required: true, rows: 14, value: CONTENT_TEMPLATES[task.task_type] || {} }],
        onSubmit: async (v) => {
          const sub = await api("POST", `/tasks/${taskId}/submissions`, { expert_id: me.expert_id, content: v.content });
          result.replaceChildren(el("p", {}, "Submitted. Status: ", badge(sub.status)),
            sub.reviews.map((r) => el("details", {}, el("summary", {}, `${label(r.kind)}: ${r.verdict}`), el("pre", {}, JSON.stringify(r.detail, null, 2)))));
          toast("Submission received");
        },
      }),
      result),
  ];
}

// ------------------------------------------------------------------ views: users

async function viewUsers() {
  const [users, firms, experts] = await Promise.all([api("GET", "/users"), api("GET", "/firms"), api("GET", "/experts")]);
  const firmName = Object.fromEntries(firms.map((f) => [f.id, f.name]));
  const expertName = Object.fromEntries(experts.map((e) => [e.id, e.name]));
  const keySlot = el("div", {});
  return [
    pageHead("Users", "People who can sign in. Each user gets one API key, shown once at creation."),
    card(`${users.length} user${users.length === 1 ? "" : "s"}`, table([
      { label: "Name", render: (u) => el("div", {}, el("div", {}, u.name), el("div", { class: "muted" }, u.email)) },
      { label: "Role", render: (u) => label(u.role) },
      { label: "Linked to", render: (u) => firmName[u.firm_id] || expertName[u.expert_id] || "—" },
      { label: "Status", render: (u) => (u.active ? el("span", { class: "badge ok" }, "Active") : el("span", { class: "badge bad" }, "Revoked")) },
      { label: "", render: (u) => (u.active && u.id !== me.id ? el("button", { type: "button", class: "small secondary", onclick: async () => {
        if (!confirm(`Revoke access for ${u.name}? Their API keys stop working immediately.`)) return;
        try { await api("POST", `/users/${u.id}/revoke`); toast("Access revoked"); refresh(); } catch (err) { toast(err.message, "bad"); }
      } }, "Revoke") : "") },
    ], users)),
    card("Invite a user",
      el("p", { class: "muted" }, "Client users must belong to a firm. Reviewers and experts must be linked to an expert record."),
      form({
        submitLabel: "Create user",
        fields: [
          { name: "name", label: "Name", required: true },
          { name: "email", label: "Email", type: "email", required: true },
          { name: "role", label: "Role", type: "select", required: true, options: ROLES },
          { name: "firm_id", label: "Firm", type: "select", placeholder: "None", options: firms.map((f) => ({ value: f.id, label: f.name })) },
          { name: "expert_id", label: "Expert record", type: "select", placeholder: "None", options: experts.map((e) => ({ value: e.id, label: e.name })) },
        ],
        onSubmit: async (v) => {
          const res = await api("POST", "/users", { ...v, firm_id: v.firm_id || null, expert_id: v.expert_id || null });
          keySlot.replaceChildren(secretReveal(`Key for ${v.name}`, res.api_key, () => refresh()));
        },
      }),
      keySlot),
  ];
}

// ------------------------------------------------------------------ boot

window.addEventListener("hashchange", route);

(async function boot() {
  if (!sessionStorage.getItem(KEY_STORAGE)) return renderSignIn();
  try {
    me = await api("GET", "/me");
    route();
  } catch {
    sessionStorage.removeItem(KEY_STORAGE);
    renderSignIn();
  }
})();
