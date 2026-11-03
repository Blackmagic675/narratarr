"use strict";
/* Narratarr — single-file SPA, no build step.
 * All HTTP goes through api(), which is where auth (token/cookie, 401 → login) will plug in. */

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, signal } = {}) {
  const opts = { method, signal, headers: {}, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // Auth hook: once auth exists, route to a login view here.
    toast("Not authenticated", "bad");
    throw new ApiError(401, "Not authenticated");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new ApiError(res.status, (data && data.detail) || res.statusText);
  return data;
}

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
}

const names = (arr) => (arr || []).map((a) => (typeof a === "string" ? a : a.name)).filter(Boolean).join(", ");
const seriesLabel = (s) => (s.sequence ? `${s.name} #${s.sequence}` : s.name);
function fmtRuntime(min) { if (!min) return null; const h_ = Math.floor(min / 60), m = min % 60; return h_ ? `${h_}h ${m}m` : `${m}m`; }
function fmtSecs(s) { return s ? fmtRuntime(Math.round(s / 60)) : "—"; }
function fmtSize(b) { if (!b) return "—"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return `${b.toFixed(i > 1 ? 1 : 0)} ${u[i]}`; }
function ago(ts) { const s = Date.now() / 1000 - ts; if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)}m ago`; if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return `${Math.floor(s / 86400)}d ago`; }
const STATUS_LABEL = { queued: "Queued", searching: "Searching", needs_review: "Needs review", downloading: "Downloading", processing: "Processing", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
const status = (s) => h("span", { class: `status s-${s}` }, STATUS_LABEL[s] || s);
const initials = (t) => (t || "?").replace(/^(the|a|an)\s+/i, "").slice(0, 1).toUpperCase();

function cover(url, title, cls = "") {
  const c = h("div", { class: `cover ${cls}` }, h("span", {}, initials(title)));
  if (url) {
    // Attached immediately: detached lazy images never load. The initial shows until it does.
    const img = h("img", { src: url, alt: "", loading: "lazy", decoding: "async" });
    img.onerror = () => img.remove();
    c.append(img);
  }
  return c;
}
function confirmDialog(title, message, okLabel = "Continue") {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; closeModal(); resolve(v); };
    openModal(h("div", { class: "modal small" },
      h("header", {}, h("div", {}, h("h2", { style: { margin: 0 } }, title))),
      h("div", { class: "body" },
        h("p", { class: "muted", style: { marginTop: "10px" } }, message),
        h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "18px" } },
          h("button", { class: "btn ghost", onclick: () => finish(false) }, "Cancel"),
          h("button", { class: "btn primary", onclick: () => finish(true) }, okLabel)))),
      () => finish(false));
  });
}
async function reencodeFlow(url, { title, size, duration, files }) {
  let kbps = 64, merge = true, choices = { 64: "64 kbps", 96: "96 kbps", 128: "128 kbps" };
  try {
    const st = await api("/api/system/status");
    kbps = st.reencode_kbps || 64; merge = st.reencode_merge !== false; choices = st.reencode_choices || choices;
  } catch { /* defaults */ }
  if (!choices[kbps]) choices = { ...choices, [kbps]: `${kbps} kbps` };
  const many = files && files > 1;
  const rate = h("select", { style: { width: "100%" } }, Object.entries(choices).map(([k, label]) => h("option", { value: k, selected: Number(k) === kbps }, label)));
  const msg = h("p", { class: "muted", style: { marginTop: "10px" } });
  const box = h("input", { type: "checkbox" });
  box.checked = merge;
  const describe = () => {
    kbps = Number(rate.value);
    const est = duration ? duration * kbps * 1000 / 8 : null;
    const sizeLine = est ? ` At most about ${fmtSize(est)}${size ? ` (now ${fmtSize(size)})` : ""}.` : "";
    msg.replaceChildren(box.checked
    ? `${many ? `Joins the ${files} files into one M4B` : "Converts it to M4B"} at ${kbps} kbps, with chapters.${sizeLine} The original files are replaced, so their names aren't kept.`
    : `Re-encodes ${many ? "each file that's above" : "the file if it's above"} ${kbps} kbps on its own, keeping names and folders (MP3 stays MP3; other formats become AAC .m4a).${sizeLine} The originals are replaced.`);
  };
  box.addEventListener("change", describe);
  rate.addEventListener("change", describe);
  describe();
  const ok = await new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; closeModal(); resolve(v); };
    openModal(h("div", { class: "modal small" },
      h("header", {}, h("div", {}, h("h2", { style: { margin: 0 } }, `Re-encode “${title}”`))),
      h("div", { class: "body" },
        h("label", { class: "row", style: { gap: "8px", marginTop: "10px", cursor: "pointer" } }, box,
          many ? "Join into one M4B with chapters" : "Convert to M4B"),
        h("label", { style: { display: "block", marginTop: "12px" } },
          h("div", { class: "faint", style: { fontSize: "12px", marginBottom: "4px" } }, "Bitrate"), rate),
        msg,
        h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "18px" } },
          h("button", { class: "btn ghost", onclick: () => finish(false) }, "Cancel"),
          h("button", { class: "btn primary", onclick: () => finish(true) }, "Re-encode")))),
      () => finish(false));
  });
  if (!ok) return false;
  try {
    await api(url, { method: "POST", body: { merge: box.checked, kbps: Number(rate.value) } });
    toast("Re-encoding — progress is in the sidebar and the Queue", "ok");
    refreshActivity();
    return true;
  } catch (err) { toast(err.message, "bad"); return false; }
}
function absPill(ref) {
  if (!ref) return null;
  return h("a", { class: "pill abs", href: ref.link, target: "_blank", rel: "noopener", title: "Open in Audiobookshelf" }, "✓ In Audiobookshelf library");
}

function scoreEl(s) { return h("span", { class: `score ${s >= 72 ? "hi" : s >= 50 ? "mid" : "lo"}` }, Math.round(s)); }

// ------------------------------------------------------------------- router
const routes = { search: renderSearch, queue: renderQueue, library: renderLibrary, import: renderImport, settings: renderSettings };
let pageCleanup = null;

function navigate(path, push = true) {
  if (push) history.pushState({}, "", path);
  route();
}

function route() {
  if (pageCleanup) { pageCleanup(); pageCleanup = null; }
  closeModal();
  const page = location.pathname.split("/")[1] || "search";
  const render = routes[page] || renderSearch;
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  const view = $("#view");
  view.replaceChildren();
  render(view);
  view.focus({ preventScroll: true });
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (a && !e.metaKey && !e.ctrlKey) { e.preventDefault(); navigate(a.getAttribute("href")); }
});
window.addEventListener("popstate", () => route());

// ------------------------------------------------------------------- modal
function openModal(content, onClose) {
  closeModal();
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) closeModal(); } }, content);
  back._onClose = onClose;
  $("#modal-root").append(back);
  document.addEventListener("keydown", escClose);
}
function closeModal() {
  const back = $("#modal-root .modal-back");
  if (back) { back._onClose && back._onClose(); back.remove(); }
  document.removeEventListener("keydown", escClose);
}
function escClose(e) { if (e.key === "Escape") closeModal(); }

// ================================================================== SEARCH
let lastSearch = { q: "", results: null, media: "audiobook" };

function renderSearch(view) {
  const input = h("input", { type: "search", placeholder: "Title, author, narrator or ASIN…", value: lastSearch.q, autofocus: true });
  const results = h("div", { class: "results" });
  const info = h("div", { class: "muted", style: { marginBottom: "10px" } });
  const form = h("form", { class: "searchbar", onsubmit: (e) => { e.preventDefault(); run(); } }, input, h("button", { class: "btn primary" }, "Search"));
  const MEDIA = [["audiobook", "Audiobooks"], ["ebook", "Ebooks"]];
  const mediaTabs = h("div", { class: "tabs media-tabs", role: "tablist", "aria-label": "What to search for" }, MEDIA.map(([m, label]) =>
    h("button", { type: "button", "data-media": m, class: lastSearch.media === m ? "on" : "", onclick: () => setMedia(m) }, label)));
  function setMedia(m) {
    if (lastSearch.media === m) return;
    lastSearch.media = m;
    mediaTabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.media === m));
    input.placeholder = m === "ebook" ? "Title, author or ISBN…" : "Title, author, narrator or ASIN…";
    if (input.value.trim()) run(); else clear();
  }
  if (lastSearch.media === "ebook") input.placeholder = "Title, author or ISBN…";
  let seq = 0;      // ignores responses from searches that were superseded or cleared
  let rows = [];    // [{ book, setState }] for live status updates

  function clear() {
    seq++;
    lastSearch = { q: "", results: null, media: lastSearch.media };
    rows = [];
    info.textContent = "";
    results.replaceChildren();
  }
  // The search box's ✕ (and deleting all the text) resets the page
  input.addEventListener("input", () => { if (!input.value.trim()) clear(); });

  async function run() {
    const q = input.value.trim();
    if (!q) { clear(); return; }
    const mine = ++seq;
    lastSearch.q = q;
    results.replaceChildren(h("div", { class: "muted" }, h("span", { class: "spinner" }), "  Searching…"));
    info.textContent = "";
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(q)}&media=${lastSearch.media}`);
      if (mine !== seq) return;
      lastSearch.results = data;
      draw(data);
    } catch (e) {
      if (mine !== seq) return;
      results.replaceChildren(h("div", { class: "empty" }, h("b", {}, "Lookup failed"), e.message));
    }
  }

  function draw(data) {
    const list = data.results || [];
    const srcs = `from ${data.sources} source${data.sources === 1 ? "" : "s"}`;
    info.textContent = list.length ? `${list.length} result${list.length === 1 ? "" : "s"} ${srcs}` +
      (data.fallback ? " · primary source didn't respond, showing backup source only" : "") : "";
    lastSearch.multi = data.configured_sources > 1;
    if (!list.length) { rows = []; results.replaceChildren(h("div", { class: "empty" }, h("b", {}, "No matches"), "Try fewer words, or search by ASIN.")); return; }
    rows = list.map((b) => ({ book: b, ...resultRow(b) }));
    results.replaceChildren(...rows.map((r) => r.el));
  }

  // Keep queued / in-library / Audiobookshelf badges current (removed jobs, finished downloads…)
  async function refreshStatus() {
    if (!rows.length) return;
    const mine = seq;
    try {
      const st = await api("/api/search/status", { method: "POST", body: { media: lastSearch.media, items: rows.map((r) => ({ asin: r.book.asin, ref: r.book.ref, title: r.book.title, authors: r.book.authors })) } });
      if (mine !== seq) return;
      for (const r of rows) {
        const x = st[r.book.ref || r.book.asin];
        if (!x) continue;
        const changed = JSON.stringify([r.book.in_library, r.book.job, r.book.in_abs]) !== JSON.stringify([x.in_library, x.job, x.in_abs]);
        Object.assign(r.book, x);
        if (changed) r.setState();
      }
    } catch { /* ignore */ }
  }

  view.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Search"), h("div", { class: "sub" }, "Find a release, then request it from Soulseek.")), mediaTabs),
    form, info, results,
  );
  if (lastSearch.results && (lastSearch.results.media || "audiobook") === lastSearch.media) { draw(lastSearch.results); refreshStatus(); }
  const timer = setInterval(refreshStatus, 4000);
  pageCleanup = () => clearInterval(timer);
}

function resultRow(b) {
  const ebook = b.media === "ebook";
  const act = h("div", { class: "actions" });
  const setState = () => {
    act.replaceChildren();
    if (b.in_abs) act.append(absPill(b.in_abs));
    if (b.in_library) act.append(h("span", { class: "pill", style: { color: "var(--ok)" } }, ebook ? "✓ Ebook in Narratarr" : "✓ In Narratarr library"));
    const owned = b.in_library || b.in_abs;
    if (b.job && !["completed", "cancelled"].includes(b.job.status)) act.append(h("a", { class: "btn small ghost", href: "/queue", "data-link": true }, status(b.job.status)));
    else act.append(h("button", { class: `btn ${owned ? "" : "primary"}`, onclick: request }, owned ? "Request again" : ebook ? "Request ebook" : "Request"));
  };
  async function request(e) {
    const btnEl = e.target;
    if (b.in_abs) {
      const go = await confirmDialog("Already in Audiobookshelf",
        `“${b.in_abs.title}” is already in your Audiobookshelf ${ebook ? "ebook " : ""}library. Request it anyway?`, "Request anyway");
      if (!go) return;
    }
    btnEl.disabled = true;
    try {
      const job = await api("/api/jobs", { method: "POST", body: { asin: b.asin, ref: b.ref, region: b.region, book: b, media: ebook ? "ebook" : "audiobook" } });
      b.job = { id: job.id, status: job.status };
      toast(`Queued “${b.title}”`, "ok");
      refreshBadge();
    } catch (err) { toast(err.message, "bad"); }
    setState();
  }
  setState();
  const descEl = b.description ? h("div", { class: "desc", onclick: (e) => e.currentTarget.classList.toggle("open") }, b.description) : null;
  const abridged = !ebook && b.format && b.format.toLowerCase() === "abridged";
  const el = h("article", { class: "result" },
    cover(b.cover_url, b.title),
    h("div", {},
      h("div", { class: "title" }, b.title),
      b.subtitle ? h("div", { class: "subtitle" }, b.subtitle) : null,
      h("div", { class: "credits" },
        h("div", {}, h("span", {}, "By "), h("b", {}, names(b.authors) || "Unknown")),
        !ebook && b.narrators?.length ? h("div", {}, h("span", {}, "Narrated by "), h("b", {}, names(b.narrators))) : null),
      h("div", { class: "meta" },
        (b.series || []).map((s) => h("span", { class: "pill series" }, seriesLabel(s))),
        !ebook && fmtRuntime(b.runtime_min) ? h("span", { class: "pill" }, fmtRuntime(b.runtime_min)) : null,
        ebook && b.isbn ? h("span", { class: "pill mono" }, `ISBN ${b.isbn}`) : null,
        b.year ? h("span", { class: "pill" }, b.year) : null,
        abridged ? h("span", { class: "pill warn" }, "Abridged") : null,
        !ebook && b.region ? h("span", { class: "pill region" }, b.region) : null,
        !ebook && b.asin ? h("span", { class: "pill mono" }, b.asin) : null,
        lastSearch.multi && b.sources ? h("span", { class: "pill", title: "Metadata sources for this result" }, `${b.sources.length} source${b.sources.length === 1 ? "" : "s"}`) : null),
      descEl),
    act);
  return { el, setState };
}

// =================================================================== QUEUE
let queueScope = "active";

function renderQueue(view) {
  const list = h("div", { class: "jobs" });
  const tabs = h("div", { class: "tabs" }, ["active", "history"].map((s) =>
    h("button", { class: queueScope === s ? "on" : "", onclick: () => { queueScope = s; tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent.toLowerCase() === s)); load(); } }, s[0].toUpperCase() + s.slice(1))));
  const clear = h("button", { class: "btn small ghost", onclick: async () => { await api("/api/jobs/clear", { method: "POST" }); load(); } }, "Clear finished");

  view.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Queue"), h("div", { class: "sub" }, "Searches run one at a time; sources are tried in order until one completes.")),
      h("div", { class: "row" }, clear, tabs)),
    list);

  async function load() {
    try {
      const jobs = await api(`/api/jobs?scope=${queueScope}`);
      if (!jobs.length) {
        list.replaceChildren(h("div", { class: "empty" }, h("b", {}, queueScope === "active" ? "Nothing in progress" : "No history yet"),
          queueScope === "active" ? h("span", {}, "Request something from ", h("a", { href: "/search", "data-link": true }, "Search"), ".") : null));
        return;
      }
      list.replaceChildren(...jobs.map(jobCard));
    } catch (e) { list.replaceChildren(h("div", { class: "empty" }, h("b", {}, "Couldn't load the queue"), e.message)); }
  }

  function jobCard(j) {
    const acts = [];
    const btn = (label, fn, cls = "small") => h("button", { class: `btn ${cls}`, onclick: async (e) => { e.stopPropagation(); e.target.disabled = true; try { await fn(); } catch (err) { toast(err.message, "bad"); } load(); } }, label);
    acts.push(h("button", { class: `btn small ${j.status === "needs_review" ? "primary" : ""}`, onclick: () => openJob(j.id, load) }, j.status === "needs_review" ? "Pick source" : "Details"));
    if (["failed", "cancelled", "needs_review"].includes(j.status)) acts.push(btn("Retry", () => api(`/api/jobs/${j.id}/retry`, { method: "POST" })));
    if (["queued", "searching", "needs_review", "downloading"].includes(j.status)) acts.push(btn("Cancel", () => api(`/api/jobs/${j.id}/cancel`, { method: "POST" }), "small ghost danger"));
    if (j.files_missing && j.status === "needs_review") {
      acts.push(btn("Re-download missing", () => api(`/api/jobs/${j.id}/redownload-missing`, { method: "POST" }), "small primary"));
      acts.push(btn("Import what's here", () => api(`/api/jobs/${j.id}/import-partial`, { method: "POST" })));
    }
    if (j.status === "completed" && j.abs_status === "pending") acts.push(btn("Send to Audiobookshelf", () => api(`/api/jobs/${j.id}/abs`, { method: "POST" })));
    if (j.abs_link) acts.push(h("a", { class: "btn small", href: j.abs_link, target: "_blank", rel: "noopener" }, "Open in Audiobookshelf"));
    if (["completed", "failed", "cancelled"].includes(j.status)) acts.push(btn("Remove", () => api(`/api/jobs/${j.id}`, { method: "DELETE" }), "small ghost"));
    const showProgress = ["downloading", "processing"].includes(j.status);
    return h("div", { class: "job" },
      cover(j.cover_url, j.title),
      h("div", {},
        h("div", { class: "row" }, status(j.status), j.media === "ebook" ? h("span", { class: "pill ebook" }, "Ebook") : null, j.kind === "import" ? h("span", { class: "pill" }, "Manual import") : j.kind === "reencode" ? h("span", { class: "pill" }, "Re-encode") : null, h("span", { class: "faint", style: { fontSize: "12px" } }, ago(j.created_at))),
        h("div", { class: "title", style: { marginTop: "4px" } }, j.title),
        h("div", { class: "muted", style: { fontSize: "13px" } }, names(j.authors), j.narrators?.length ? ` · read by ${names(j.narrators)}` : "", j.series?.length ? ` · ${seriesLabel(j.series[0])}` : ""),
        h("div", { class: "detail" }, j.error && j.status === "failed" ? j.error : j.detail),
        j.selected && ["downloading", "processing"].includes(j.status) ? h("div", { class: "faint", style: { fontSize: "12px", marginTop: "2px" } }, `from ${j.selected.username} · ${j.selected.display_dir}`) : null,
        j.status === "completed" && j.abs_status ? h("div", { class: `abs-line ${j.abs_status}` }, j.abs_status === "imported" ? "Audiobookshelf: " : "Audiobookshelf pending: ", j.abs_detail || "") : null,
        showProgress ? h("div", { class: "progress" }, h("i", { style: { width: `${j.progress || 0}%` } })) : null),
      h("div", { class: "actions" }, acts));
  }

  load();
  const timer = setInterval(() => { if (!$("#modal-root .modal-back")) load(); }, 2500);
  pageCleanup = () => clearInterval(timer);
}

async function openJob(id, onChange) {
  const body = h("div", { class: "body" }, h("div", { class: "muted", style: { padding: "20px 0" } }, h("span", { class: "spinner" }), " Loading…"));
  const headerEl = h("header", {});
  openModal(h("div", { class: "modal" }, headerEl, body), () => onChange && onChange());
  let timer = null;
  // Built once, outside the refresh, so polling never steals focus or wipes what you're typing.
  const q = h("input", { type: "text", placeholder: "Custom slskd search, e.g. author title" });
  const searchBtn = h("button", { class: "btn" }, "Search slskd");
  const customForm = h("form", { class: "row", style: { marginTop: "14px" }, onsubmit: async (e) => {
    e.preventDefault();
    const query = q.value.trim();
    if (query.length < 3) { toast("Type at least 3 characters", "bad"); return; }
    searchBtn.disabled = true;
    try { await api(`/api/jobs/${id}/search`, { method: "POST", body: { query } }); toast("Searching…"); load(); }
    catch (err) { toast(err.message, "bad"); }
    searchBtn.disabled = false;
  } }, h("div", { class: "grow" }, q), searchBtn);
  const dynamic = h("div", {});
  let shown = false;


  async function load() {
    let j;
    try { j = await api(`/api/jobs/${id}`); } catch (e) { shown = false; body.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    headerEl.replaceChildren(
      cover(j.cover_url, j.title),
      h("div", {},
        status(j.status),
        h("h2", { style: { margin: "4px 0 2px" } }, j.title),
        h("div", { class: "muted" }, names(j.authors), j.narrators?.length ? ` · read by ${names(j.narrators)}` : "", j.runtime_min ? ` · ${fmtRuntime(j.runtime_min)}` : "", j.asin ? ` · ${j.asin}` : ""),
        h("div", { class: "detail muted", style: { marginTop: "6px", fontSize: "13px" } }, j.error || j.detail)),
      h("button", { class: "btn ghost close", onclick: closeModal }, "✕"));

    const cands = j.candidates || [];
    const tried = new Set((j.selected && j.selected.tried) || []);
    const table = cands.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Score", "Source", "Files", "Size", "Length", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, cands.map((c) => h("tr", {},
        h("td", {}, scoreEl(c.score)),
        h("td", {},
          h("div", { class: "path" }, c.display_dir),
          h("div", { class: "faint", style: { fontSize: "12px" } }, `${c.username}${c.free_slot ? " · free slot" : ` · queue ${c.queue_length}`} · ${fmtSize(c.upload_speed)}/s`),
          h("div", { class: "reasons" }, (c.reasons || []).join(" · ")),
          c.subset ? h("div", { class: "reasons" }, "subset of a multi-book folder") : null),
        h("td", {}, c.file_count, " ", h("span", { class: "faint" }, (c.formats || []).join("/"))),
        h("td", {}, fmtSize(c.size)),
        h("td", {}, fmtSecs(c.length_s)),
        h("td", {}, j.selected && j.selected.id === c.id && ["downloading", "processing", "completed"].includes(j.status)
          ? h("span", { class: "pill" }, "Selected")
          : h("button", { class: `btn small ${tried.has(c.id) ? "" : "primary"}`, disabled: ["processing", "completed", "searching"].includes(j.status), onclick: async (e) => {
              e.target.disabled = true;
              try { await api(`/api/jobs/${id}/select`, { method: "POST", body: { candidate_id: c.id } }); toast("Download started", "ok"); load(); } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
            } }, tried.has(c.id) ? "Retry" : "Download")))))) : h("div", { class: "empty" }, j.status === "searching" ? "Searching…" : "No candidates yet.");

    const transfers = (j.transfers || []).length ? h("table", {},
      h("thead", {}, h("tr", {}, ["File", "State", "Progress"].map((t) => h("th", {}, t)))),
      h("tbody", {}, j.transfers.map((t) => h("tr", {},
        h("td", { class: "path" }, (t.rel ? t.rel + "/" : "") + t.name),
        h("td", {}, t.local ? "Moved to incomplete" : t.state),
        h("td", {}, t.local ? "100%" : `${Math.round(t.percent || 0)}%`))))) : null;

    const attempts = (j.attempts || []).length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Query", "Peers", "Folders", "Best"].map((t) => h("th", {}, t)))),
      h("tbody", {}, j.attempts.map((a) => h("tr", {}, h("td", { class: "mono" }, a.query), h("td", {}, a.responses), h("td", {}, a.candidates), h("td", {}, scoreEl(a.best)))))) : null;

    if (!shown) { body.replaceChildren(customForm, dynamic); shown = true; }
    dynamic.replaceChildren(...[
      transfers ? [h("h3", {}, "Transfers"), transfers] : null,
      h("h3", {}, `Candidates (${cands.length})`), table,
      attempts ? [h("h3", {}, "Searches"), attempts] : null,
      j.output_path ? [h("h3", {}, "Output"), h("div", { class: "path" }, j.output_path),
        j.abs_status ? h("div", { class: `abs-line ${j.abs_status}`, style: { marginTop: "6px" } },
          j.abs_status === "imported" ? "Audiobookshelf: " : "Audiobookshelf pending: ", j.abs_detail || "",
          j.abs_link ? [" · ", h("a", { href: j.abs_link, target: "_blank", rel: "noopener" }, "Open in Audiobookshelf")] : null) : null] : null,
      h("h3", {}, "Log"),
      h("div", { class: "log" }, (j.log || []).slice().reverse().map((l) => h("div", {}, h("time", {}, new Date(l.t * 1000).toLocaleTimeString()), l.msg))),
    ].flat().filter(Boolean));
    clearTimeout(timer);
    if (["searching", "downloading", "processing", "queued"].includes(j.status)) timer = setTimeout(() => { if ($("#modal-root .modal-back")) load(); }, 3000);
  }
  load();
}

// ================================================================= LIBRARY
let libScope = "all";

function renderLibrary(view) {
  const body = h("div", {});
  const filter = h("input", { type: "search", placeholder: "Filter by title, author, narrator, series…", oninput: () => draw() });
  const tabs = h("div", { class: "tabs" });
  const sub = h("div", { class: "sub" }, "Loading…");
  let data = { narratarr: [], audiobookshelf: [], abs: {} };

  const actions = h("div", { class: "row" });
  const rescan = h("button", { class: "btn small", onclick: async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/library/rescan", { method: "POST" }); toast(`Rescan: ${r.added} added, ${r.updated} updated, ${r.removed} removed`, "ok"); load(); } catch (err) { toast(err.message, "bad"); }
    e.target.disabled = false;
  } }, "Rescan completed/");
  const sync = h("button", { class: "btn small", onclick: async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/abs/sync", { method: "POST" }); toast(`Audiobookshelf: ${r.items} books`, "ok"); load(); } catch (err) { toast(err.message, "bad"); }
    e.target.disabled = false;
  } }, "Sync Audiobookshelf");

  view.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Library"), sub), actions),
    h("div", { class: "row", style: { marginBottom: "22px" } }, h("div", { style: { flex: "1 1 280px", maxWidth: "420px" } }, filter), tabs),
    body);

  const match = (b, q) => !q || [b.title, b.subtitle, names(b.authors), names(b.narrators), (b.series || []).map((s) => s.name).join(" ")].join(" ").toLowerCase().includes(q);

  function narratarrTile(b) {
    const chip = b.abs_status === "imported" ? h("div", { class: "chip ok" }, "In Audiobookshelf")
      : b.abs_status === "pending" ? h("div", { class: "chip warn" }, "Audiobookshelf pending") : null;
    return h("div", { class: "tile", onclick: () => openBook(b.id, load) },
      h("div", { class: "cover-wrap" }, cover(b.has_cover ? `/api/library/${b.id}/cover` : null, b.title), chip,
        b.media === "ebook" ? h("div", { class: "chip ebook-chip" }, "Ebook") : null),
      h("div", { class: "t" }, b.title),
      h("div", { class: "a" }, names(b.authors)),
      b.narrators?.length ? h("div", { class: "n" }, `Read by ${names(b.narrators)}`) : null,
      b.series?.length ? h("div", { class: "n", style: { color: "var(--accent)" } }, seriesLabel(b.series[0])) : null);
  }
  function absTile(b) {
    return h("div", { class: "tile abs-tile", onclick: () => openAbsBook(b.id) },
      h("div", { class: "cover-wrap" }, cover(b.has_cover ? `/api/abs/items/${b.id}/cover` : null, b.title),
        h("div", { class: "source-strip" }, (b.via_narratarr ? "Audiobookshelf · via Narratarr" : "Audiobookshelf") + (b.media === "ebook" ? " · Ebook" : ""))),
      h("div", { class: "t" }, b.title),
      h("div", { class: "a" }, names(b.authors)),
      b.narrators?.length ? h("div", { class: "n" }, `Read by ${names(b.narrators)}`) : null,
      b.series?.length ? h("div", { class: "n" }, seriesLabel(b.series[0])) : null);
  }
  function section(title, note, items, tileFn, emptyMsg) {
    return h("section", { class: "lib-section" },
      h("div", { class: "lib-head" }, h("h2", {}, title), h("span", { class: "faint" }, note)),
      items.length ? h("div", { class: "grid" }, items.map(tileFn)) : h("div", { class: "empty" }, emptyMsg));
  }

  function draw() {
    const q = filter.value.toLowerCase();
    const em = data.narratarr.filter((b) => match(b, q));
    const ab = data.audiobookshelf.filter((b) => match(b, q));
    const absOn = data.abs.enabled;
    tabs.replaceChildren(...[["all", "All"], ["narratarr", `Narratarr (${data.narratarr.length})`], ...(absOn ? [["abs", `Audiobookshelf (${data.audiobookshelf.length})`]] : [])]
      .map(([k, label]) => h("button", { class: libScope === k ? "on" : "", onclick: () => { libScope = k; draw(); } }, label)));
    actions.replaceChildren(...(absOn ? [sync, rescan] : [rescan]));
    sub.textContent = absOn
      ? `${data.narratarr.length} in Narratarr · ${data.audiobookshelf.length} in Audiobookshelf` + (data.abs.last_sync ? ` · synced ${ago(data.abs.last_sync)}` : "") + (data.abs.error ? ` · ⚠ ${data.abs.error}` : "")
      : `${data.narratarr.length} book${data.narratarr.length === 1 ? "" : "s"} in completed/`;
    const parts = [];
    if (libScope !== "abs") parts.push(section("Narratarr", "in completed/", em, narratarrTile, data.narratarr.length ? "No matches" : "Nothing waiting in Narratarr — finished books go straight to Audiobookshelf."));
    if (absOn && libScope !== "narratarr") parts.push(section("Audiobookshelf library", "read-only view", ab, absTile, data.audiobookshelf.length ? "No matches" : "Audiobookshelf library is empty, or not synced yet."));
    body.replaceChildren(...parts);
  }
  async function load() {
    try { data = await api("/api/library"); if (!data.abs.enabled && libScope === "abs") libScope = "all"; draw(); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
  }
  load();
}

async function openBook(id, onChange) {
  const b = await api(`/api/library/${id}`);
  const book = b.book || {};
  const del = async (files) => {
    const ok = await confirmDialog(files ? "Delete files?" : "Remove from list?",
      files ? `Delete the folder and all files?\n${b.path}` : "Remove from Narratarr's library list? Files stay on disk.", files ? "Delete" : "Remove");
    if (!ok) return;
    try { await api(`/api/library/${id}?delete_files=${files}`, { method: "DELETE" }); onChange(); } catch (e) { toast(e.message, "bad"); }
  };
  const absState = await api("/api/abs/status").catch(() => ({}));
  const send = absState.enabled && b.abs_status !== "imported" ? h("button", { class: "btn primary", onclick: async (e) => {
    e.target.disabled = true;
    try { await api(`/api/library/${id}/abs`, { method: "POST" }); toast("Sending to Audiobookshelf — progress is on the Queue page", "ok"); closeModal(); } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
  } }, b.abs_status === "pending" ? "Retry Audiobookshelf" : "Send to Audiobookshelf") : null;
  const absLink = b.abs_link ? h("a", { class: "btn", href: b.abs_link, target: "_blank", rel: "noopener" }, "Open in Audiobookshelf") : null;
  openModal(h("div", { class: "modal" },
    h("header", {},
      cover(b.cover_path ? `/api/library/${id}/cover` : null, b.title),
      h("div", {},
        h("h2", { style: { margin: "0 0 2px" } }, b.title),
        b.subtitle ? h("div", { class: "muted", style: { fontStyle: "italic" } }, b.subtitle) : null,
        h("div", { class: "credits" },
          h("div", {}, h("span", {}, "By "), h("b", {}, names(b.authors))),
          b.narrators?.length ? h("div", {}, h("span", {}, "Narrated by "), h("b", {}, names(b.narrators))) : null),
        h("div", { class: "meta" },
          (b.series || []).map((s) => h("span", { class: "pill series" }, seriesLabel(s))),
          h("span", { class: "pill" }, fmtSecs(b.duration_s)),
          h("span", { class: "pill" }, `${b.file_count} file${b.file_count === 1 ? "" : "s"}`),
          h("span", { class: "pill" }, fmtSize(b.size_bytes)),
          b.asin ? h("span", { class: "pill mono" }, b.asin) : null,
          b.abs_status === "imported" ? h("span", { class: "pill abs" }, "✓ In Audiobookshelf library") : b.abs_status === "pending" ? h("span", { class: "pill warn" }, "Audiobookshelf pending") : null)),
      h("button", { class: "btn ghost close", onclick: closeModal }, "✕")),
    h("div", { class: "body" },
      book.description ? h("p", { class: "muted", style: { whiteSpace: "pre-line", maxWidth: "80ch" } }, book.description) : null,
      h("h3", {}, "Folder"), h("div", { class: "path" }, b.path),
      h("h3", {}, "Files"), h("div", { class: "log" }, (b.files || []).map((f) => h("div", {}, f))),
      h("div", { class: "row", style: { marginTop: "18px" } },
        send, absLink,
        h("button", { class: "btn", onclick: async () => {
          if (await reencodeFlow(`/api/library/${id}/reencode`, { title: b.title, size: b.size_bytes, duration: b.duration_s, files: b.file_count })) closeModal();
        } }, "Re-encode"),
        h("span", { class: "grow" }),
        h("button", { class: "btn ghost", onclick: () => del(false) }, "Remove from list"),
        h("button", { class: "btn ghost danger", onclick: () => del(true) }, "Delete files")))));
}

async function openAbsBook(itemId) {
  let b;
  try { b = await api(`/api/abs/items/${itemId}`); } catch (e) { toast(e.message, "bad"); return; }
  openModal(h("div", { class: "modal abs-modal" },
    h("header", {},
      cover(b.has_cover ? `/api/abs/items/${itemId}/cover` : null, b.title),
      h("div", {},
        h("div", { class: "source-label" }, b.via_narratarr ? "Audiobookshelf · imported by Narratarr" : "Audiobookshelf"),
        h("h2", { style: { margin: "2px 0 2px" } }, b.title),
        b.subtitle ? h("div", { class: "muted", style: { fontStyle: "italic" } }, b.subtitle) : null,
        h("div", { class: "credits" },
          h("div", {}, h("span", {}, "By "), h("b", {}, names(b.authors) || "Unknown")),
          b.narrators?.length ? h("div", {}, h("span", {}, "Narrated by "), h("b", {}, names(b.narrators))) : null),
        h("div", { class: "meta" },
          (b.series || []).map((s) => h("span", { class: "pill series" }, seriesLabel(s))),
          h("span", { class: "pill" }, fmtSecs(b.duration_s)),
          b.num_tracks ? h("span", { class: "pill" }, `${b.num_tracks} track${b.num_tracks === 1 ? "" : "s"}`) : null,
          b.num_chapters ? h("span", { class: "pill" }, `${b.num_chapters} chapters`) : null,
          b.year ? h("span", { class: "pill" }, b.year) : null,
          b.asin ? h("span", { class: "pill mono" }, b.asin) : h("span", { class: "pill warn" }, "No ASIN"))),
      h("button", { class: "btn ghost close", onclick: closeModal }, "✕")),
    h("div", { class: "body" },
      b.description ? h("p", { class: "muted", style: { whiteSpace: "pre-line", maxWidth: "80ch" } }, b.description) : null,
      h("h3", {}, "Folder in Audiobookshelf"), h("div", { class: "path" }, b.path || "—"),
      h("div", { class: "row", style: { marginTop: "18px" } },
        h("a", { class: "btn primary", href: b.link, target: "_blank", rel: "noopener" }, "Open in Audiobookshelf"),
        h("button", { class: "btn", onclick: async () => {
          if (await reencodeFlow(`/api/abs/items/${itemId}/reencode`, { title: b.title, size: b.size_bytes, duration: b.duration_s, files: b.num_tracks })) closeModal();
        } }, "Re-encode")))));
}

// ============================================================ MANUAL IMPORT
let importRoot = "incomplete";

function renderImport(view) {
  const rootTabs = h("div", { class: "tabs" });
  const folders = h("div", {});
  const matchPanel = h("div", { class: "panel" }, h("div", { class: "muted" }, "Pick a folder on the left to match it to a release."));
  let current = null;

  view.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Manual Import"), h("div", { class: "sub" }, "Match a folder of audio to a release, then tag and file it into completed/.")),
      h("div", { class: "row" }, rootTabs, h("button", { class: "btn small ghost", onclick: () => loadFolders() }, "Refresh"))),
    h("div", { class: "split" }, h("div", { class: "panel", style: { padding: "8px" } }, folders), matchPanel));

  async function loadRoots() {
    const roots = await api("/api/import/roots");
    if (!roots.find((r) => r.key === importRoot)) importRoot = roots[0].key;
    rootTabs.replaceChildren(...roots.map((r) => h("button", { class: r.key === importRoot ? "on" : "", title: r.path + (r.exists ? "" : " (missing)"),
      onclick: () => { importRoot = r.key; loadRoots(); } }, r.label + (r.exists ? "" : " ⚠"))));
    loadFolders();
  }

  async function loadFolders() {
    folders.replaceChildren(h("div", { class: "muted", style: { padding: "12px" } }, h("span", { class: "spinner" }), " Scanning…"));
    try {
      const list = await api(`/api/import/scan?root=${importRoot}`);
      if (!list.length) { folders.replaceChildren(h("div", { class: "empty", style: { border: 0 } }, h("b", {}, "No audio folders here"))); return; }
      folders.replaceChildren(...list.map((f) => {
        const el = h("div", { class: "folder", onclick: () => { folders.querySelectorAll(".folder").forEach((x) => x.classList.remove("on")); el.classList.add("on"); select(f); } },
          h("div", {},
            h("div", { class: "nm" }, f.name, f.already_imported ? h("span", { class: "pill", style: { marginLeft: "6px" } }, "imported") : null),
            h("div", { class: "pt" }, f.rel === "." ? "(root)" : f.rel)),
          h("div", { class: "faint", style: { fontSize: "12px", textAlign: "right" } }, `${f.files} × ${f.formats.join("/")}`, h("br"), `${fmtSize(f.size)} · ${ago(f.mtime)}`));
        return el;
      }));
    } catch (e) { folders.replaceChildren(h("div", { class: "empty", style: { border: 0 } }, h("b", {}, "Can't read folder"), e.message)); }
  }

  function select(f) {
    current = f;
    const q = h("input", { type: "search", value: f.suggestion });
    const results = h("div", {});
    const mode = h("select", {}, h("option", { value: "move" }, "Move files"), h("option", { value: "copy" }, "Copy files"));
    async function find() {
      results.replaceChildren(h("div", { class: "muted", style: { padding: "10px 0" } }, h("span", { class: "spinner" }), " Searching…"));
      try {
        const data = await api(`/api/search?q=${encodeURIComponent(q.value)}`);
        if (!data.results.length) { results.replaceChildren(h("div", { class: "muted", style: { padding: "10px 0" } }, "No matches — edit the query.")); return; }
        results.replaceChildren(...data.results.slice(0, 12).map((b) => h("div", { class: "mini" },
          cover(b.cover_url, b.title),
          h("div", {},
            h("div", { class: "t" }, b.title),
            h("div", { class: "muted", style: { fontSize: "12.5px" } }, names(b.authors), b.narrators?.length ? ` · read by ${names(b.narrators)}` : ""),
            h("div", { class: "faint", style: { fontSize: "12px" } }, [b.series?.[0] ? seriesLabel(b.series[0]) : null, fmtRuntime(b.runtime_min), b.year, b.region ? b.region.toUpperCase() : null].filter(Boolean).join(" · ")),
            b.in_abs ? h("div", { style: { marginTop: "4px" } }, absPill(b.in_abs)) : null),
          h("button", { class: "btn small primary", onclick: async (e) => {
            e.target.disabled = true;
            try {
              await api("/api/import", { method: "POST", body: { root: importRoot, rel: current.rel, asin: b.asin, ref: b.ref, region: b.region, mode: mode.value, book: b } });
              toast(`Importing “${b.title}” — see Queue`, "ok");
              refreshBadge();
              setTimeout(loadFolders, 1500);
            } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
          } }, "Import"))));
      } catch (e) { results.replaceChildren(h("div", { class: "muted" }, e.message)); }
    }
    matchPanel.replaceChildren(
      h("div", { class: "nm", style: { fontWeight: 600, wordBreak: "break-word" } }, f.name),
      h("div", { class: "faint", style: { fontSize: "12px", marginBottom: "12px" } }, `${f.files} files · ${fmtSize(f.size)} · filenames are kept as-is`),
      h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); find(); } }, h("div", { class: "grow" }, q), mode, h("button", { class: "btn" }, "Find")),
      results);
    find();
  }

  loadRoots().catch((e) => folders.replaceChildren(h("div", { class: "empty" }, e.message)));
}

// ================================================================ SETTINGS
async function renderSettings(view) {
  view.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("div", { class: "sub" }, "Connections, matching and file handling. Saved to /config/narratarr.db.")),
    h("button", { class: "btn", type: "button", onclick: () => openWelcome() }, "Welcome guide")));
  const healthBox = h("div", { class: "health-box" });
  view.append(healthBox);
  api("/api/system/health").then((hs) => {
    const list = hs.health.issues || [];
    healthBox.className = `health-box ${hs.health.level}`;
    healthBox.replaceChildren(h("span", { class: `dot ${hs.health.level}` }),
      list.length ? h("div", {}, h("b", {}, list.length === 1 ? "1 thing needs attention" : `${list.length} things need attention`),
        h("ul", {}, list.map((x) => h("li", {}, x.text))))
        : h("div", {}, h("b", {}, "Everything is working"), h("span", { class: "muted" }, " · slskd connected, folders writable")));
  }).catch(() => healthBox.remove());
  const statusBox = h("div", { class: "statusbox" });
  const form = h("form", { onsubmit: (e) => { e.preventDefault(); save(); } });
  view.append(statusBox, form);
  const inputs = {};

  const [schema, sys] = await Promise.all([api("/api/settings"), api("/api/system/status")]);
  const pathCell = (label, p) => h("div", { class: "panel" }, h("div", { class: "k" }, label), h("div", { class: "v path" }, p.path),
    h("div", { style: { fontSize: "12px", color: p.exists && p.writable ? "var(--ok)" : "var(--bad)" } }, !p.exists ? "missing" : p.writable ? "ok · writable" : "not writable"));
  statusBox.append(
    pathCell("Incomplete", sys.paths.incomplete), pathCell("Completed", sys.paths.completed), pathCell("slskd downloads (mounted)", sys.paths.slskd_downloads),
    h("div", { class: "panel" }, h("div", { class: "k" }, "Runtime"), h("div", { class: "v" }, `Narratarr ${/^\d/.test(sys.version) ? "v" : ""}${sys.version}`),
      h("div", { style: { fontSize: "12px", color: sys.ffmpeg ? "var(--ok)" : "var(--warn)" } }, sys.ffmpeg ? "ffmpeg available (m4b chapter embedding)" : "ffmpeg missing — m4b chapters skipped")));

  const groups = {};
  for (const s of schema) if (s.type !== "hidden") (groups[s.group] ||= []).push(s);
  const tests = { slskd: "slskd", Metadata: "metadata", Audiobookshelf: "abs" };

  for (const [group, items] of Object.entries(groups)) {
    const result = h("span", { class: "test-result" });
    const head = h("header", {}, h("h2", {}, group));
    if (tests[group]) head.append(h("div", { class: "row" }, result, h("button", { type: "button", class: "btn small", onclick: async (e) => {
      e.target.disabled = true; result.className = "test-result"; result.replaceChildren(h("span", { class: "spinner" }));
      try {
        await save(true);
        const r = await api(`/api/settings/test/${tests[group]}`, { method: "POST" });
        result.className = `test-result ${r.ok ? "ok" : "bad"}`;
        result.textContent = r.message;
        if (r.libraries) fillAbs(r.libraries);
      } catch (err) { result.className = "test-result bad"; result.textContent = err.message; }
      e.target.disabled = false;
    } }, "Save & test")));
    const sec = h("section", { class: "settings-group" }, head);
    for (const s of items) {
      let input;
      if (s.type === "bool") {
        input = h("input", { type: "checkbox", checked: !!s.value });
        sec.append(h("div", { class: "field" }, h("div", {}, h("label", {}, s.label), s.help ? h("div", { class: "help" }, s.help) : null), h("label", { class: "switch" }, input, h("span"))));
      } else {
        if (s.type === "select" || s.options?.length) {
          const opts = s.options.map(String);
          if (s.value !== "" && s.value != null && !opts.includes(String(s.value))) opts.push(String(s.value));
          input = h("select", {}, opts.map((o) => h("option", { value: o, selected: o === String(s.value) },
            s.labels?.[o] ?? (o === "" ? "(none)" : s.type === "int" ? `${o} (custom)` : o))));
        }
        else if (s.type === "abs_library" || s.type === "abs_folder") {
          input = h("select", {}, h("option", { value: s.value || "" }, s.value ? `${s.value} (Save & test to load names)` : "Save & test to load"));
          if (s.type === "abs_library") input.addEventListener("change", () => fillFolders());
        }
        else input = h("input", { type: s.type === "secret" ? "password" : s.type === "int" ? "number" : "text", value: s.value ?? "", autocomplete: "off" });
        sec.append(h("div", { class: "field" }, h("div", {}, h("label", {}, s.label), s.help ? h("div", { class: "help" }, s.help) : null), input));
      }
      input.dataset.type = s.type;
      inputs[s.key] = input;
    }
    form.append(sec);
  }
  form.append(h("div", { class: "sticky-save" }, h("button", { class: "btn primary" }, "Save settings")));

  // Audiobookshelf library / folder pickers
  let absLibs = [];
  function fillAbs(libs) {
    absLibs = libs;
    const books = libs.filter((l) => l.mediaType === "book");
    const sel = inputs["abs.library_id"];
    const cur = sel.value;
    sel.replaceChildren(h("option", { value: "" }, "— choose a library —"),
      ...books.map((l) => h("option", { value: l.id, selected: l.id === cur }, `${l.name}`)));
    if (!books.find((l) => l.id === cur) && books.length === 1) sel.value = books[0].id;
    const esel = inputs["abs.ebook_library_id"];
    if (esel) {
      const ecur = esel.value;
      esel.replaceChildren(h("option", { value: "" }, "— none: keep ebooks in Narratarr —"),
        ...books.map((l) => h("option", { value: l.id, selected: l.id === ecur }, `${l.name}`)));
    }
    fillFolders();
  }
  function fillFolders() {
    for (const [libKey, folderKey] of [["abs.library_id", "abs.folder_id"], ["abs.ebook_library_id", "abs.ebook_folder_id"]]) {
      const sel = inputs[folderKey];
      if (!sel || !inputs[libKey]) continue;
      const cur = sel.value;
      const lib = absLibs.find((l) => l.id === inputs[libKey].value);
      const folders = lib ? lib.folders : [];
      sel.replaceChildren(...(folders.length ? folders.map((f) => h("option", { value: f.id, selected: f.id === cur }, f.path))
        : [h("option", { value: "" }, lib ? "Library has no folders" : "Choose a library first")]));
      if (folders.length && !folders.find((f) => f.id === cur)) sel.value = folders[0].id;
    }
  }
  if (inputs["abs.token"]?.value) api("/api/abs/libraries").then(fillAbs).catch(() => {});

  async function save(quiet = false) {
    const body = {};
    for (const [k, el] of Object.entries(inputs)) body[k] = el.type === "checkbox" ? el.checked : el.value;
    await api("/api/settings", { method: "PUT", body });
    if (!quiet) toast("Settings saved", "ok");
  }
}

// ------------------------------------------------------------- activity panel
const ACT_ICON = { searching: "search", downloading: "down", processing: "gear", abs: "up", sync: "sync",
  completed: "ok", failed: "bad", needs_review: "warn" };
function actIcon(kind) {
  const k = ACT_ICON[kind] || "gear";
  const paths = {
    search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/>',
    down: '<path d="M8 2v9M4.5 7.5L8 11l3.5-3.5M3 14h10"/>',
    up: '<path d="M8 13V4M4.5 7.5L8 4l3.5 3.5M3 2h10"/>',
    gear: '<circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/>',
    sync: '<path d="M13 6A5 5 0 0 0 3.5 4.5M3 10a5 5 0 0 0 9.5 1.5"/><path d="M3 2v3h3M13 14v-3h-3"/>',
    ok: '<path d="M3 8.5l3 3 7-7"/>', bad: '<path d="M4 4l8 8M12 4l-8 8"/>', warn: '<path d="M8 3v6M8 12v.5"/>',
  };
  const el = h("span", { class: `act-icon ${k}` });
  el.innerHTML = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths[k]}</svg>`;
  return el;
}
async function refreshActivity() {
  const box = $("#activity");
  let data;
  try { data = await api("/api/activity"); } catch { return; }
  const items = data.items || [];
  const rows = items.slice(0, 5).map((it) => h("div", {
      class: `act-row ${it.kind}`, title: `${it.title} — ${it.text}`,
      onclick: () => { if (it.id) { if (location.pathname !== "/queue") navigate("/queue"); openJob(it.id); } } },
    actIcon(it.kind),
    h("div", { class: "act-body" },
      h("div", { class: "act-title" }, it.title),
      h("div", { class: "act-text" }, it.text),
      it.progress != null && it.progress > 0 && it.progress < 100 ? h("div", { class: "act-bar" }, h("i", { style: { width: `${it.progress}%` } })) : null)));
  if (data.queued) rows.push(h("div", { class: "act-row queued", onclick: () => navigate("/queue") }, actIcon("gear"),
    h("div", { class: "act-body" }, h("div", { class: "act-text" }, `${data.queued} waiting in the queue`))));
  box.replaceChildren(...rows);
  box.hidden = !rows.length;
}

// ------------------------------------------------------------- background
async function refreshBadge() {
  try {
    const jobs = await api("/api/jobs?scope=active");
    const b = $("#queue-badge");
    const review = jobs.filter((j) => j.status === "needs_review").length;
    b.hidden = !jobs.length;
    b.textContent = jobs.length;
    b.style.background = review ? "var(--warn)" : "";
    b.title = review ? `${review} need review` : "";
  } catch { /* ignore */ }
}
// ------------------------------------------------- footer: version, health, updates
const vlabel = (v) => `${/^\d/.test(v) ? "v" : ""}${v}`;
let lastSystem = null;

async function refreshFoot() {
  const foot = $("#sidebar-foot");
  try {
    const s = lastSystem = await api("/api/system/health");
    const issues = s.health.issues || [];
    const tip = issues.length ? issues.map((i) => `• ${i.text}`).join("\n") : "Everything is working";
    const rows = [h("a", { class: "foot-line", href: "/settings", "data-link": "", title: tip },
      h("span", { class: `dot ${s.health.level}` }), vlabel(s.version),
      issues.length ? h("span", { class: `foot-issues ${s.health.level}` }, `\u00a0· ${issues.length} issue${issues.length === 1 ? "" : "s"}`) : null)];
    if (s.update.available) {
      rows.push(h("button", { class: "foot-update", onclick: () => openUpdateModal(s.update) },
        `Update available: v${s.update.latest.version}`));
    }
    foot.replaceChildren(...rows);
    return s;
  } catch {
    foot.replaceChildren(h("div", { class: "foot-line" }, h("span", { class: "dot bad" }), "Server unreachable"));
    return null;
  }
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = h("textarea", { style: { position: "fixed", opacity: "0" } });
  ta.value = text; document.body.append(ta); ta.select();
  try { document.execCommand("copy"); } finally { ta.remove(); }
  return Promise.resolve();
}
function cmdBlock(cmd) {
  return h("div", { class: "cmd" }, h("code", {}, cmd),
    h("button", { class: "btn ghost small", type: "button", onclick: async (e) => {
      await copyText(cmd); e.target.textContent = "Copied"; setTimeout(() => { e.target.textContent = "Copy"; }, 1500);
    } }, "Copy"));
}

// Minimal Markdown for GitHub's generated release notes: headings, bullets, **bold**, links.
function mdInline(text) {
  const out = [];
  const re = /\*\*(.+?)\*\*|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s)]+)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("b", {}, m[1]));
    else {
      const url = m[3] || m[4];
      const pr = url.match(/\/pull\/(\d+)$/);
      out.push(h("a", { href: url, target: "_blank", rel: "noopener" }, m[2] || (pr ? `#${pr[1]}` : url.replace(/^https:\/\/github\.com\//, ""))));
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function mdBlock(md) {
  const box = h("div", { class: "release-notes" });
  let list = null;
  for (const raw of (md || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const head = line.match(/^#{1,6}\s+(.*)$/);
    const item = line.match(/^[*-]\s+(.*)$/);
    if (head) { list = null; box.append(h("h4", {}, ...mdInline(head[1]))); }
    else if (item) { if (!list) { list = h("ul"); box.append(list); } list.append(h("li", {}, ...mdInline(item[1]))); }
    else { list = null; box.append(h("p", {}, ...mdInline(line))); }
  }
  return box;
}

function openUpdateModal(u) {
  const image = "ghcr.io/joshbosh/narratarr:latest";
  const date = u.latest.published_at ? new Date(u.latest.published_at).toLocaleDateString() : null;
  openModal(h("div", { class: "modal small update-modal" },
    h("header", {}, h("div", {},
      h("h2", { style: { margin: 0 } }, `Narratarr v${u.latest.version} is available`),
      h("div", { class: "muted", style: { fontSize: "13px", marginTop: "2px" } }, `You're on ${vlabel(u.current)}${date ? ` · released ${date}` : ""}`)),
      h("button", { class: "btn ghost close", onclick: closeModal }, "✕")),
    h("div", { class: "body" },
      u.latest.notes ? mdBlock(u.latest.notes) : null,
      h("h3", {}, "Docker Compose"),
      h("p", { class: "muted" }, "In the folder with your docker-compose.yml:"),
      cmdBlock("docker compose pull"), cmdBlock("docker compose up -d"),
      h("h3", {}, "Docker CLI"),
      cmdBlock(`docker pull ${image}`), cmdBlock("docker stop narratarr"), cmdBlock("docker rm narratarr"),
      h("p", { class: "muted" }, "Then run your original docker run command again."),
      h("p", { class: "faint", style: { fontSize: "12.5px" } }, "Settings, the queue and your library are kept in /config, so nothing is lost."),
      h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "16px" } },
        h("a", { class: "btn", href: u.latest.url, target: "_blank", rel: "noopener" }, "View release on GitHub"),
        h("button", { class: "btn primary", onclick: closeModal }, "Done")))));
}

// ------------------------------------------------- first-run welcome guide
async function openWelcome() {
  let sys = lastSystem;
  try { sys = await api("/api/system/health"); } catch { /* show anyway */ }
  const issues = sys?.health?.issues || [];
  const about = (prefix) => issues.filter((i) => i.text.toLowerCase().startsWith(prefix));
  const statusLine = (list, okText, todoText) => list.length
    ? h("div", { class: "welcome-status warn" }, "To do: ", list.map((i) => i.text).join(" · "))
    : h("div", { class: "welcome-status ok" }, okText || todoText);
  const absOn = (await api("/api/settings").catch(() => [])).find?.((x) => x.key === "abs.enabled")?.value;
  const updates = h("input", { type: "checkbox", checked: true });
  const flow = h("div", { class: "welcome-flow" },
    ...["Search", "Request", "slskd downloads", "Tagged + chapters", "Audiobookshelf"].flatMap((t, i, a) =>
      [h("span", { class: "pill" }, t), i < a.length - 1 ? h("span", { class: "faint" }, "→") : null]).filter(Boolean));
  const slides = [
    { title: "Welcome to Narratarr", body: [
      h("p", {}, "Find an audiobook by its Audible release and request it. Narratarr searches Soulseek through slskd, picks the best match, downloads it, tags it with cover art and chapters, and files it into Audiobookshelf."),
      flow,
      h("p", { class: "muted" }, "This quick guide covers what to set up. You can reopen it from Settings at any time.")] },
    { title: "Connect slskd", body: [
      h("p", {}, "Narratarr downloads through your slskd instance. In ", h("b", {}, "Settings → slskd"), " you'll need:"),
      h("ul", {}, h("li", {}, "slskd's URL, reachable from this container"),
        h("li", {}, "an API key with the ", h("b", {}, "readwrite"), " role (from slskd.yml)"),
        h("li", {}, "slskd's downloads folder mounted into Narratarr at /slskd-downloads")),
      statusLine([...about("slskd"), ...issues.filter((i) => /folder/i.test(i.text))], "slskd is connected and the folders are writable.")] },
    { title: "Audiobookshelf (optional)", body: [
      h("p", {}, "Finished books can go straight into an Audiobookshelf library, with metadata filled in and duplicates flagged in Search. In ", h("b", {}, "Settings → Audiobookshelf"), ":"),
      h("ul", {}, h("li", {}, "mount your Audiobookshelf library folder into Narratarr"),
        h("li", {}, "add an admin user's API key, then pick the library and folder")),
      absOn ? statusLine(about("audiobookshelf"), "Audiobookshelf is connected.")
        : h("div", { class: "welcome-status" }, "Not set up. Without it, finished books stay in Narratarr's completed folder.")] },
    { title: "Good to know", body: [h("ul", { class: "welcome-tips" },
      h("li", {}, h("b", {}, "Needs review: "), "when no source is a confident match, the job waits for you to pick one or run a custom search."),
      h("li", {}, h("b", {}, "Activity: "), "the bottom of the sidebar shows what's happening right now."),
      h("li", {}, h("b", {}, "Hardcover: "), "an optional second metadata source that fills gaps (free API key, Settings → Metadata)."),
      h("li", {}, h("b", {}, "Re-encode: "), "shrink books to a lean bitrate, automatically or with the Re-encode button in the Library."),
      h("li", {}, h("b", {}, "Health light: "), "the dot next to the version turns amber or red when something needs attention; hover it for details."))] },
    { title: "Stay up to date", body: [
      h("label", { class: "field welcome-toggle" },
        h("div", {}, h("div", {}, h("b", {}, "Check for updates")),
          h("div", { class: "help" }, "Narratarr asks GitHub for the latest release a few times a day and shows “Update available” in the sidebar. Nothing else is sent. You can change this in Settings → General.")),
        h("span", { class: "switch" }, updates, h("span"))),
      sys && sys.update && !sys.update.release_build
        ? h("p", { class: "faint", style: { fontSize: "12.5px" } }, "This is a development build, so it won't check until you run a release.") : null] },
  ];
  let i = 0;
  const title = h("h2", { style: { margin: 0 } });
  const body = h("div", { class: "welcome-body" });
  const dots = h("div", { class: "welcome-dots" });
  const back = h("button", { class: "btn ghost", type: "button" }, "Back");
  const next = h("button", { class: "btn primary", type: "button" });
  let saved = false;
  const persist = () => {
    if (saved) return;
    saved = true;
    api("/api/settings", { method: "PUT", body: { "general.welcome_done": true, "general.check_updates": updates.checked } })
      .then(() => refreshFoot()).catch(() => { /* shown again next visit */ });
  };
  const finish = () => { persist(); closeModal(); };
  const show = () => {
    title.textContent = slides[i].title;
    body.replaceChildren(...slides[i].body.filter(Boolean));
    dots.replaceChildren(...slides.map((_, n) => h("button", { type: "button", class: n === i ? "on" : "", "aria-label": `Step ${n + 1}`, onclick: () => { i = n; show(); } })));
    back.hidden = i === 0;
    next.textContent = i === slides.length - 1 ? "Finish" : "Next";
  };
  back.addEventListener("click", () => { i = Math.max(0, i - 1); show(); });
  next.addEventListener("click", () => { if (i === slides.length - 1) finish(); else { i += 1; show(); } });
  openModal(h("div", { class: "modal small welcome" },
    h("header", {}, title, h("button", { class: "btn ghost close", title: "Skip the guide", onclick: finish }, "✕")),
    h("div", { class: "body" }, body,
      h("div", { class: "welcome-nav" }, dots, h("span", { class: "grow" }),
        h("button", { class: "btn ghost", type: "button", onclick: finish }, "Skip"), back, next))),
    persist);
  show();
}

route();
refreshBadge(); refreshActivity();
refreshFoot().then((s) => { if (s && !s.welcome_done) openWelcome(); });
setInterval(refreshBadge, 5000);
setInterval(refreshActivity, 2500);
setInterval(refreshFoot, 30000);
