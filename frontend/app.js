/* Tamil Movie Automator — SPA frontend (no framework, no build step) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  if (opts.json) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
    delete opts.json;
  }
  const r = await fetch("/api" + path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 4000);
}

/* ── routing ────────────────────────────────────────────────────────── */
const pages = ["dashboard", "library", "search", "review", "settings", "logs"];
function navigate() {
  const page = (location.hash || "#dashboard").slice(1).split("/")[0];
  const p = pages.includes(page) ? page : "dashboard";
  pages.forEach((x) => $("#page-" + x).classList.toggle("hidden", x !== p));
  document.querySelectorAll(".nav-link").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === p));
  ({ dashboard: loadDashboard, library: () => loadLibrary(true),
     review: loadReview, settings: loadSettings, logs: loadLogs }[p] || (() => {}))();
}
window.addEventListener("hashchange", navigate);

/* ── shared widgets ─────────────────────────────────────────────────── */
function movieCard(m) {
  const poster = m.poster
    ? `<img src="${esc(m.poster)}" loading="lazy" alt="">`
    : `<div class="poster-ph">🎬</div>`;
  const rating = m.rating ? `<span class="rating">★ ${m.rating.toFixed(1)}</span>` : "";
  const tamil = m.is_tamil_original ? `<span class="tamil-pill">TAMIL</span>` : "";
  return `<div class="movie-card" onclick="openMovie(${m.id})">
    <span class="status-pill st-${esc(m.status)}">${esc(m.status.replace("_", " "))}</span>
    ${tamil}${poster}
    <div class="info">
      <div class="t" title="${esc(m.title)}">${esc(m.matched_title || m.title)}</div>
      <div class="meta"><span>${m.year ?? ""}</span>${rating}</div>
    </div></div>`;
}

function torrentRow(t, actionHtml) {
  const tags = [t.quality, t.codec, t.rip_type, t.file_size,
                (t.languages || []).join("+"), t.is_magnet ? "magnet" : "file"]
    .filter(Boolean).map((x) => `<span class="quality-tag">${esc(x)}</span>`).join(" ");
  return `<div class="torrent-row"><div>${tags}</div>
    <div class="name small">${esc(t.name)}</div>${actionHtml || ""}</div>`;
}

function candidateRow(c, i, movieId) {
  const img = c.poster_url ? `<img src="${esc(c.poster_url)}" loading="lazy">` : "";
  return `<div class="candidate">${img}
    <div><div><b>${esc(c.title)}</b> (${c.year ?? "?"})</div>
      <div class="muted small">${esc(c.source)} · lang: ${esc(c.original_language || c.language_names || "?")}
        ${c.rating ? " · ★ " + c.rating : ""}${c._evidence ? " · " + esc(c._evidence) : ""}</div></div>
    <span class="score">${(c._score ?? 0).toFixed(2)}</span>
    <button class="btn sm primary" onclick="pickCandidate(${movieId}, ${i})">This one</button>
  </div>`;
}

/* ── dashboard ──────────────────────────────────────────────────────── */
async function loadDashboard() {
  try {
    const s = await api("/stats");
    const bs = s.by_status || {};
    $("#stats-cards").innerHTML = [
      ["Total movies", s.total], ["Downloaded", bs.sent || 0],
      ["Tamil originals", s.tamil_originals], ["Needs review", s.needs_review],
      ["Rejected", bs.rejected || 0], ["Library", bs.library || 0],
      ["In Radarr", bs.in_radarr || 0],
    ].map(([l, n]) => `<div class="stat-card"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");
    $("#domain-indicator").textContent = "Domain: " + (s.current_domain || "?");
    const badge = $("#review-badge");
    badge.classList.toggle("hidden", !s.needs_review);
    badge.textContent = s.needs_review;
    renderFullScan(s.full_scan);
    const recent = await api("/movies?per_page=12");
    $("#recent-movies").innerHTML = recent.movies.map(movieCard).join("");
  } catch (e) { toast("Dashboard failed: " + e.message, true); }
}

function renderFullScan(fs) {
  if (!fs) return;
  const pct = fs.total_pages ? Math.round((fs.last_page / fs.total_pages) * 100) : 0;
  $("#fullscan-status").innerHTML = fs.running
    ? `<span class="spinner"></span> Running — page ${fs.last_page}/${fs.total_pages || "?"} (${pct}%), ${fs.cataloged} movies cataloged`
    : `Idle — checkpoint at page ${fs.last_page}${fs.total_pages ? "/" + fs.total_pages : ""}, ${fs.cataloged} movies cataloged so far`;
}

let fsTimer = null;
function pollFullScan() {
  clearInterval(fsTimer);
  fsTimer = setInterval(async () => {
    try {
      const fs = await api("/library/full-scan/status");
      renderFullScan(fs);
      if (!fs.running) clearInterval(fsTimer);
    } catch { clearInterval(fsTimer); }
  }, 4000);
}

$("#btn-scan").onclick = async () => {
  try { const r = await api("/scan", { method: "POST" });
    toast(r.ok ? "Scan started — check Logs for progress" : r.note); }
  catch (e) { toast(e.message, true); }
};
$("#btn-radarr-sync").onclick = async () => {
  toast("Syncing Radarr library…");
  try { const r = await api("/system/radarr-sync", { method: "POST" });
    toast(r.ok ? `Radarr sync ✓ — ${r.imported} imported, ${r.linked} linked (of ${r.radarr_total})`
               : "Sync failed: " + r.error, !r.ok);
    loadDashboard(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-domain").onclick = async () => {
  toast("Checking domain…");
  try { const r = await api("/system/domain-check", { method: "POST" });
    toast(r.ok ? "Domain OK: " + r.domain : "Domain discovery failed — see Logs", !r.ok);
    loadDashboard(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-domain-force").onclick = async () => {
  toast("Launching Chrome search — this can take a couple of minutes…");
  try { const r = await api("/system/domain-check?force=true", { method: "POST" });
    toast(r.ok ? "Domain found: " + r.domain : "Search failed — see Logs", !r.ok);
    loadDashboard(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-fullscan-start").onclick = async () => {
  try { const r = await api("/library/full-scan/start", { method: "POST" });
    toast(r.ok ? "Full scan started" : r.note); pollFullScan(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-fullscan-stop").onclick = async () => {
  await api("/library/full-scan/stop", { method: "POST" });
  toast("Stop requested — finishing current page");
};
$("#btn-fullscan-reset").onclick = async () => {
  if (!confirm("Reset full-scan progress to page 0?")) return;
  await api("/library/full-scan/reset", { method: "POST" });
  toast("Progress reset"); loadDashboard();
};

/* ── library ────────────────────────────────────────────────────────── */
let libPage = 1, libTotal = 0;
async function loadLibrary(reset) {
  if (reset) { libPage = 1; $("#lib-grid").innerHTML = ""; }
  const params = new URLSearchParams({ page: libPage, per_page: 40, sort: $("#lib-sort").value });
  if ($("#lib-status").value) params.set("status", $("#lib-status").value);
  if ($("#lib-tamil").value) params.set("tamil", $("#lib-tamil").value);
  if ($("#lib-search").value.trim()) params.set("q", $("#lib-search").value.trim());
  try {
    const r = await api("/movies?" + params);
    libTotal = r.total;
    $("#lib-grid").insertAdjacentHTML("beforeend", r.movies.map(movieCard).join(""));
    $("#lib-more").classList.toggle("hidden", libPage * 40 >= libTotal);
  } catch (e) { toast(e.message, true); }
}
["lib-status", "lib-tamil", "lib-sort"].forEach((id) =>
  $("#" + id).addEventListener("change", () => loadLibrary(true)));
let libTimer;
$("#lib-search").addEventListener("input", () => {
  clearTimeout(libTimer); libTimer = setTimeout(() => loadLibrary(true), 350);
});
$("#lib-more").onclick = () => { libPage++; loadLibrary(false); };

/* ── movie detail modal ─────────────────────────────────────────────── */
window.openMovie = async (id) => {
  try {
    const m = await api("/movies/" + id);
    const poster = m.poster ? `<img src="${esc(m.poster)}">` : "";
    const kv = (k, v) => v != null && v !== "" ? `<div class="kv"><b>${k}</b>${v}</div>` : "";
    const torrents = (m.torrents || []).map((t) =>
      torrentRow(t, `<button class="btn sm primary" onclick="dlTorrent(${m.id}, ${t.id})">Download</button>`)).join("");
    const cands = (m.candidates || []).map((c, i) => candidateRow(c, i, m.id)).join("");
    $("#modal-content").innerHTML = `
      <div class="detail-head">${poster}<div>
        <h2>${esc(m.matched_title || m.title)} ${m.year ? "(" + m.year + ")" : ""}</h2>
        ${kv("Status", `<span class="status-pill st-${esc(m.status)}" style="position:static">${esc(m.status)}</span>`)}
        ${kv("Forum title", esc(m.forum_title))}
        ${kv("Rating", m.rating ? `★ ${m.rating} <span class="muted">(${esc(m.rating_source || "")})</span>` : null)}
        ${kv("Language", esc(m.original_language) + (m.is_tamil_original ? " — Tamil original" : ""))}
        ${kv("Match confidence", m.match_confidence != null ? m.match_confidence.toFixed(2) : null)}
        ${kv("Rejection", esc(m.rejection_reason))}
        ${kv("Radarr", m.added_to_radarr ? "✓ added" : (m.radarr_skip_reason || "—"))}
        ${kv("qBittorrent", m.added_to_qbittorrent ? "✓ added" : "—")}
        ${kv("IMDb", m.imdb_id ? `<a href="https://www.imdb.com/title/${esc(m.imdb_id)}/" target="_blank">${esc(m.imdb_id)}</a>` : null)}
        ${kv("Forum", m.forum_url ? `<a href="${esc(m.forum_url)}" target="_blank">open post</a>` : null)}
        <div class="row gap" style="margin-top:12px">
          <button class="btn primary sm" onclick="dlTorrent(${m.id}, null)">Download (auto pick)</button>
          <button class="btn sm" onclick="rematch(${m.id})">Re-match metadata</button>
          <button class="btn danger sm" onclick="delMovie(${m.id})">Delete</button>
        </div>
      </div></div>
      ${cands ? `<h3 style="margin-top:18px">Match candidates</h3>${cands}` : ""}
      ${torrents ? `<h3 style="margin-top:18px">Torrents</h3>${torrents}` : "<p class='muted' style='margin-top:14px'>No torrents stored — Download will fetch the post first.</p>"}`;
    $("#modal").classList.remove("hidden");
  } catch (e) { toast(e.message, true); }
};
window.closeModal = () => $("#modal").classList.add("hidden");
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

window.dlTorrent = async (movieId, torrentId) => {
  toast("Sending to Radarr + qBittorrent…");
  try {
    const r = await api(`/movies/${movieId}/download`, { method: "POST", json: { torrent_id: torrentId } });
    toast(r.ok ? "Sent ✓" : "Failed: " + (r.error || r.status), !r.ok);
    closeModal();
  } catch (e) { toast(e.message, true); }
};
window.rematch = async (movieId) => {
  toast("Re-matching…");
  try { await api(`/movies/${movieId}/rematch`, { method: "POST" }); openMovie(movieId); }
  catch (e) { toast(e.message, true); }
};
window.delMovie = async (movieId) => {
  if (!confirm("Delete this movie from the database?")) return;
  await api("/movies/" + movieId, { method: "DELETE" });
  closeModal(); toast("Deleted"); loadLibrary(true);
};
window.pickCandidate = async (movieId, idx) => {
  try {
    const r = await api(`/movies/${movieId}/review`, { method: "POST", json: { candidate_idx: idx } });
    toast(r.ok ? "Match confirmed ✓" : r.error, !r.ok);
    if (!$("#page-review").classList.contains("hidden")) loadReview();
    else openMovie(movieId);
  } catch (e) { toast(e.message, true); }
};

/* ── search ─────────────────────────────────────────────────────────── */
async function doSearch() {
  const q = $("#search-input").value.trim();
  if (!q) return;
  $("#search-results").innerHTML = `<p class="muted"><span class="spinner"></span> Searching forum and checking torrents…</p>`;
  try {
    const r = await api("/search?q=" + encodeURIComponent(q));
    if (!r.results.length) {
      $("#search-results").innerHTML = `<p class="muted">No results with torrents found.</p>`;
      return;
    }
    $("#search-results").innerHTML = r.results.map((res) => `
      <div class="result-block">
        <b>${esc(res.forum_title)}</b>
        ${res.torrents.map((t) => torrentRow(t,
          `<button class="btn sm primary" onclick='searchDownload(${JSON.stringify(res.forum_url)}, ${JSON.stringify(res.forum_title)}, ${JSON.stringify(t.torrent_url)})'>Download</button>`)).join("")}
      </div>`).join("");
  } catch (e) {
    $("#search-results").innerHTML = "";
    toast("Search failed: " + e.message, true);
  }
}
$("#search-btn").onclick = doSearch;
$("#search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });

window.searchDownload = async (forumUrl, forumTitle, torrentUrl) => {
  toast("Adding…");
  try {
    const r = await api("/search/download", { method: "POST",
      json: { forum_url: forumUrl, forum_title: forumTitle, torrent_url: torrentUrl } });
    toast(r.ok ? "Sent to Radarr + qBittorrent ✓" : "Failed: " + (r.error || r.status), !r.ok);
  } catch (e) { toast(e.message, true); }
};

/* ── review queue ───────────────────────────────────────────────────── */
async function loadReview() {
  try {
    const r = await api("/movies?status=needs_review&per_page=50");
    if (!r.movies.length) {
      $("#review-list").innerHTML = `<p class="muted">Nothing to review 🎉</p>`;
      return;
    }
    const detailed = await Promise.all(r.movies.map((m) => api("/movies/" + m.id)));
    $("#review-list").innerHTML = detailed.map((m) => `
      <div class="result-block">
        <div class="row gap">
          <b>${esc(m.title)} ${m.year ? "(" + m.year + ")" : ""}</b>
          <span class="muted small">${esc(m.rejection_reason || "")}</span>
          <span class="grow"></span>
          <button class="btn sm" onclick="openMovie(${m.id})">Details</button>
          <button class="btn sm primary" onclick="dlTorrent(${m.id}, null)">Download anyway</button>
        </div>
        ${(m.candidates || []).map((c, i) => candidateRow(c, i, m.id)).join("")
          || "<p class='muted small'>No candidates — use Re-match after checking API keys.</p>"}
      </div>`).join("");
  } catch (e) { toast(e.message, true); }
}

/* ── settings ───────────────────────────────────────────────────────── */
const SETTINGS_GROUPS = {
  "Forum": ["website_base", "current_domain", "forum_path", "search_path", "site_fingerprints"],
  "Download preferences": ["preferred_quality", "preferred_codec", "rating_threshold", "max_size_gb"],
  "Metadata": ["tmdb_api_key", "omdb_api_key", "match_auto_accept", "match_review_floor"],
  "Radarr": ["radarr_url", "radarr_api_key", "radarr_quality_profile_id", "radarr_root_folder", "radarr_sync_enabled", "radarr_sync_time"],
  "qBittorrent": ["qbittorrent_url", "qbittorrent_username", "qbittorrent_password", "qbittorrent_category"],
  "Daily scan": ["daily_scan_enabled", "daily_scan_time", "scan_pages", "scan_max_links", "duplicate_stop_count", "auto_download"],
  "Domain & housekeeping": ["domain_check_enabled", "domain_check_time", "full_scan_delay_seconds", "log_retention_days"],
};
const BOOL_KEYS = new Set(["daily_scan_enabled", "auto_download", "domain_check_enabled", "radarr_sync_enabled"]);
const SECRET_KEYS = new Set(["tmdb_api_key", "omdb_api_key", "radarr_api_key", "qbittorrent_password"]);

async function loadSettings() {
  try {
    const s = await api("/settings");
    $("#settings-form").innerHTML = Object.entries(SETTINGS_GROUPS).map(([group, keys]) => `
      <div class="settings-group panel"><h3>${group}</h3>
        ${keys.map((k) => {
          const v = s[k] ?? "";
          const input = BOOL_KEYS.has(k)
            ? `<select class="input" data-key="${k}"><option value="true"${v === "true" ? " selected" : ""}>enabled</option><option value="false"${v !== "true" ? " selected" : ""}>disabled</option></select>`
            : `<input class="input" data-key="${k}" type="${SECRET_KEYS.has(k) ? "password" : "text"}" value="${esc(v)}">`;
          return `<div class="setting-row"><label>${k}</label>${input}</div>`;
        }).join("")}
      </div>`).join("");
  } catch (e) { toast(e.message, true); }
}
$("#settings-save").onclick = async () => {
  const values = {};
  document.querySelectorAll("#settings-form [data-key]").forEach((el) => values[el.dataset.key] = el.value);
  try {
    await api("/settings", { method: "PUT", json: values });
    $("#settings-msg").textContent = "Saved ✓ (scheduler reloaded)";
    toast("Settings saved");
  } catch (e) { toast(e.message, true); }
};
$("#test-radarr").onclick = async () => {
  try { const r = await api("/system/test-radarr", { method: "POST" });
    toast(r.ok ? "Radarr OK — profiles: " + r.profiles.map((p) => `${p.id}:${p.name}`).join(", ") : "Radarr connection failed", !r.ok); }
  catch (e) { toast(e.message, true); }
};
$("#test-qbit").onclick = async () => {
  try { const r = await api("/system/test-qbittorrent", { method: "POST" });
    toast(r.ok ? "qBittorrent OK" : "qBittorrent connection failed", !r.ok); }
  catch (e) { toast(e.message, true); }
};

/* ── logs ───────────────────────────────────────────────────────────── */
async function loadLogs() {
  try {
    const level = $("#log-level").value;
    const logs = await api("/logs?limit=300" + (level ? "&level=" + level : ""));
    $("#log-list").innerHTML = logs.map((l) => `
      <div class="log-row"><span class="lv lv-${esc(l.level)}">${esc(l.level)}</span>
        <span class="ts">${esc(l.created_at.replace("T", " ").slice(0, 19))}</span>
        <span>${esc(l.mes