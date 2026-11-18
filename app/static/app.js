/* Event Sync UI - plain JS, no build step. */
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const filters = { presence: "", conflictsOnly: false, warningsOnly: false, q: "" };
const open = new Set();
const detailCache = new Map();

const STATE_LABEL = {
  match: "match", compatible: "compatible", differs: "differs", conflict: "CONFLICT",
  only_crm: "CRM only", only_calendar: "Calendar only", missing: "missing", "n/a": "n/a",
};

const badge = (src) => {
  const map = { crm: ["crm", "CRM"], calendar: ["cal", "Calendar"], both: ["both", "Both"] };
  const [cls, label] = map[src] || ["none", "—"];
  return `<span class="badge ${cls}">${label}</span>`;
};

const sourcesCell = (m) => {
  const parts = [];
  if (m.source_ids.crm.length) parts.push(`<span class="badge crm" title="${esc(m.source_ids.crm.join(", "))}">CRM</span>`);
  if (m.source_ids.calendar.length) parts.push(`<span class="badge cal" title="${esc(m.source_ids.calendar.join(", "))}">Cal</span>`);
  const ids = [...m.source_ids.crm, ...m.source_ids.calendar].join(" · ");
  return `${parts.join("")}<small class="muted" style="display:block">${esc(ids)}</small>`;
};

const val = (v) => {
  if (v === null || v === undefined || v === "") return '<span class="muted">—</span>';
  if (Array.isArray(v)) return v.map(esc).join("<br>");
  return esc(v);
};

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

/* ---------------------------------------------------------------- summary */
async function loadSummary() {
  const s = await getJSON("/api/summary");
  const c = s.counts;
  const cards = [
    [c.meetings, "unified meetings"],
    [c.both, "in both sources"],
    [c.crm_only, "CRM only"],
    [c.calendar_only, "calendar only"],
    [c.with_conflicts, `with conflicts (${c.conflicts_total} total)`],
    [c.duplicates_merged, "duplicates merged"],
    [c.with_warnings, "with data issues"],
  ];
  $("#summary").innerHTML = cards.map(([n, l]) => `<div class="card"><b>${n}</b><span>${esc(l)}</span></div>`).join("");
  $("#assumptions").innerHTML =
    `<b>How to read this</b> — times shown in ${esc(s.timezone)}.<ul>${s.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` +
    (c.rejected ? `<p><b>${c.rejected} record(s) could not be ingested</b> — see /api/summary.</p>` : "");
}

/* ---------------------------------------------------------------- list */
function query() {
  const p = new URLSearchParams();
  if (filters.presence) p.set("presence", filters.presence);
  if (filters.conflictsOnly) p.set("has_conflicts", "true");
  if (filters.warningsOnly) p.set("has_warnings", "true");
  if (filters.q) p.set("q", filters.q);
  return p.toString();
}

async function loadMeetings() {
  const { count, meetings } = await getJSON("/api/meetings?" + query());
  $("#count").textContent = `${count} meeting${count === 1 ? "" : "s"}`;
  $("#empty").hidden = count > 0;
  const tbody = $("#meetings tbody");
  tbody.innerHTML = "";
  for (const m of meetings) {
    const tr = document.createElement("tr");
    tr.className = "row" + (open.has(m.id) ? " open" : "");
    tr.dataset.id = m.id;
    const [d, t] = m.start_local ? m.start_local.split(" ") : [m.date || "?", null];
    const flags = [
      m.conflict_count ? `<span class="pill ${m.max_severity}" title="${esc(m.conflict_fields.join(", "))}">⚠ ${m.conflict_count} conflict${m.conflict_count > 1 ? "s" : ""}</span>` : "",
      m.warning_count ? `<span class="pill warn">ℹ ${m.warning_count} data issue${m.warning_count > 1 ? "s" : ""}</span>` : "",
    ].join("");
    tr.innerHTML = `
      <td class="when">${esc(d)}<small>${t ? esc(t) : "time unknown"}</small></td>
      <td>${esc(m.title)}<small class="muted" style="display:block">${esc(m.location || "")}</small></td>
      <td>${val(m.client)}</td>
      <td>${sourcesCell(m)}</td>
      <td class="${m.status === "cancelled" ? "status-cancelled" : ""}">${val(m.status)}</td>
      <td>${flags || '<span class="muted">—</span>'}</td>`;
    tr.addEventListener("click", () => toggle(m.id, tr));
    tbody.appendChild(tr);
    if (open.has(m.id)) await showDetail(m.id, tr);
  }
}

async function toggle(id, tr) {
  if (open.has(id)) {
    open.delete(id);
    tr.classList.remove("open");
    if (tr.nextElementSibling?.classList.contains("detail")) tr.nextElementSibling.remove();
  } else {
    open.add(id);
    tr.classList.add("open");
    await showDetail(id, tr);
  }
}

/* ---------------------------------------------------------------- detail */
async function showDetail(id, tr) {
  if (!detailCache.has(id)) detailCache.set(id, await getJSON("/api/meetings/" + id));
  const m = detailCache.get(id);
  const row = document.createElement("tr");
  row.className = "detail";
  row.innerHTML = `<td colspan="6">${renderDetail(m)}</td>`;
  tr.after(row);
}

function renderDetail(m) {
  let h = "";

  // 1. why these records were joined (or not)
  if (m.presence === "both") {
    h += `<h3>How these records were matched</h3>
      <div class="callout info"><b>Match score ${m.match.score}</b>${esc(m.match.reasons.join(" · "))}</div>`;
  } else {
    const c = m.match.closest_candidate;
    const where = m.presence === "crm_only" ? "calendar" : "CRM";
    h += `<h3>Why this is ${where === "calendar" ? "CRM" : "calendar"}-only</h3>
      <div class="callout info">No matching ${where} record. ${c ? esc(c.note) + (c.id ? ` (${esc(c.id)}${c.title ? " – " + esc(c.title) : ""})` : "") : ""}</div>`;
  }

  // 2. duplicates folded together
  for (const d of m.duplicates) {
    h += `<h3>Duplicate records merged</h3>
      <div class="callout info"><b>${esc([d.primary, ...d.merged].join(" + "))}</b>${esc(d.reasons.join("; "))}.
      ${esc(d.primary)} was kept as primary (most recently created).</div>`;
  }

  // 3. conflicts
  if (m.conflicts.length) {
    h += `<h3>Conflicts between sources</h3>` + m.conflicts.map((c) =>
      `<div class="callout ${c.severity}"><b>${esc(c.label)} (${c.severity})</b>${esc(c.explanation)}</div>`).join("");
  }

  // 4. field-by-field with provenance
  h += `<h3>Field by field</h3>
    <table class="fields"><thead><tr><th>Field</th><th>${badge("crm")}</th><th>${badge("calendar")}</th>
    <th>Resolved value</th><th>Status</th></tr></thead><tbody>` +
    m.fields.map((f) => `
      <tr class="${f.state === "conflict" ? "conflict" : ""}">
        <td class="label">${esc(f.label)}</td>
        <td>${val(f.crm)}</td>
        <td>${val(f.calendar)}</td>
        <td>${f.resolved === null ? '<span class="muted">see both</span>' : val(f.resolved)}
            ${f.resolved_from ? `<br>${badge(f.resolved_from)}` : ""}</td>
        <td><span class="st ${f.state === "n/a" ? "" : f.state}">${esc(STATE_LABEL[f.state] || f.state)}</span>
            ${f.detail && f.state !== "conflict" ? `<span class="hint">${esc(f.detail)}</span>` : ""}</td>
      </tr>`).join("") + `</tbody></table>`;

  // 5. data-quality warnings
  if (m.warnings.length) {
    h += `<h3>Data-quality notes</h3><ul class="plain">` + m.warnings.map((w) =>
      `<li>${badge(w.source === "crm" ? "crm" : "calendar")} <b>${esc(w.record_id)}</b> · ${esc(w.field)} · <i>${esc(w.kind)}</i> — ${esc(w.message)}</li>`).join("") + `</ul>`;
  }

  // 6. raw
  h += `<details><summary>Raw source records</summary><pre>${esc(JSON.stringify(m.raw, null, 2))}</pre></details>`;
  return h;
}

/* ---------------------------------------------------------------- wiring */
let timer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(timer);
  timer = setTimeout(() => { filters.q = e.target.value.trim(); loadMeetings(); }, 200);
});
$("#presence").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  filters.presence = e.target.dataset.v;
  document.querySelectorAll("#presence button").forEach((b) => b.classList.toggle("on", b === e.target));
  loadMeetings();
});
$("#conflictsOnly").addEventListener("change", (e) => { filters.conflictsOnly = e.target.checked; loadMeetings(); });
$("#warningsOnly").addEventListener("change", (e) => { filters.warningsOnly = e.target.checked; loadMeetings(); });

// Deep link: /#mtg-1a2b3c4d (comma-separate several) opens those meetings on load.
location.hash.slice(1).split(",").filter(Boolean).forEach((id) => open.add(id));

Promise.all([loadSummary(), loadMeetings()]).catch((err) => {
  document.body.insertAdjacentHTML("afterbegin", `<p style="color:#c62828;padding:16px">Failed to load data: ${esc(err.message)}</p>`);
});
