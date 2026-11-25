"use strict";
// EvidenceDesk UI. Every action goes through the same /api/op/* operations the agent uses.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => (s === null || s === undefined ? "" : String(s)).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const H = { "Content-Type": "application/json", "X-EvidenceDesk-Token": window.ED_TOKEN };
let REVIEW = null, RUN = null, PROTO = null, pollTimer = null, SEL_REPORT = null;

async function op(name, args = {}) {
  const r = await fetch(`/api/op/${name}`, { method: "POST", headers: H, body: JSON.stringify(args) });
  const j = await r.json();
  if (name === "prisma_s" || name === "grammar") return j.markdown;
  if (!j.ok) { const e = new Error(j.error || "error"); e.data = j; throw e; }
  return j.result;
}
const tokenQS = () => `token=${encodeURIComponent(window.ED_TOKEN)}`;
function toast(msg, bad) { const el = document.createElement("div"); el.className = "toast" + (bad ? " bad" : ""); el.textContent = msg; $("#toasts").append(el); setTimeout(() => el.remove(), bad ? 7000 : 3200); }
const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); console.error(e); } }
function badge(status) {
  const cls = { complete: "b-ok", public: "b-ok", configured: "b-info", exact: "b-ok", semantic: "b-info", approximate: "b-warn", unsupported: "b-bad", automatic: "b-ok",
    awaiting_approval: "b-warn", queued: "b-info", running: "b-info", partial: "b-warn", failed: "b-bad", access_denied: "b-bad", disconnected: "b-bad",
    manual_required: "b-warn", manual: "b-warn", complete_with_alerts: "b-warn", skipped: "b-mute", imported: "b-ok", include: "b-ok", exclude: "b-bad", unclear: "b-warn",
    pending: "b-mute", conflict: "b-bad", retrieved: "b-ok", unavailable: "b-bad", not_sought: "b-mute", retrieved_extraction_failed: "b-warn", verified: "b-ok", proposed: "b-warn",
    entered: "b-info", rejected: "b-bad", ok: "b-ok", blocked: "b-bad", ran: "b-ok", script_ready: "b-info" }[status] || "b-mute";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

// ------------------------------------------------------------------ tabs & reviews
$$("#tabs button[data-tab]").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
// "More" menu
const moreWrap = $(".more");
$(".more-btn").onclick = (e) => { e.stopPropagation(); const o = moreWrap.classList.toggle("open"); $(".more-btn").setAttribute("aria-expanded", o); };
document.addEventListener("click", (e) => { if (!moreWrap.contains(e.target)) moreWrap.classList.remove("open"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") moreWrap.classList.remove("open"); });
// sliding liquid indicator under the active nav item
function moveIndicator() {
  const ind = $(".nav-indicator"), pill = $("#tabs");
  const act = $("#tabs > button.active") || ($(".more-menu button.active") ? $(".more-btn") : null);
  if (!act) { ind.style.opacity = 0; return; }
  const a = act.getBoundingClientRect(), p = pill.getBoundingClientRect();
  ind.style.opacity = 1; ind.style.width = a.width + "px"; ind.style.transform = `translateX(${a.left - p.left}px)`;
}
window.addEventListener("resize", moveIndicator);
document.fonts && document.fonts.ready.then(moveIndicator);
// pointer-following sheen on glass surfaces
let sheenRAF = 0, sheenEv = null;
document.addEventListener("pointermove", (e) => {
  sheenEv = e; if (sheenRAF) return;
  sheenRAF = requestAnimationFrame(() => {
    sheenRAF = 0;
    const el = sheenEv.target.closest && sheenEv.target.closest(".glass, .card, .tile, .src");
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${sheenEv.clientX - r.left}px`); el.style.setProperty("--my", `${sheenEv.clientY - r.top}px`);
  });
}, { passive: true });
// numbers glide to their new value instead of jumping
const lastNum = new Map();
function countUp(root) {
  $$("[data-count]", root).forEach((el) => {
    const key = el.dataset.key || el.dataset.count, to = Number(el.dataset.count);
    if (!Number.isFinite(to)) return;
    const from = lastNum.has(key) ? lastNum.get(key) : 0; lastNum.set(key, to);
    if (from === to || matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = fmt(to); return; }
    const t0 = performance.now(), dur = 1100;
    const step = (now) => { const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 4);
      el.textContent = fmt(Math.round(from + (to - from) * e)); if (k < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  });
}
function showTab(t) {
  const swap = () => {
    $$("#tabs button[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${t}`));
    moveIndicator();
  };
  moreWrap.classList.remove("open");
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) document.startViewTransition(swap); else swap();
  localStorage.setItem("ed_tab", t);
  const loaders = { protocol: loadProtocol, search: loadSearchTab, records: loadRecords, studies: loadStudies, screening: loadQueue,
    adjudication: loadAdj, fulltext: loadFT, extraction: loadExtraction, prisma: loadPrisma, settings: loadSources };
  if (REVIEW || t === "settings") guard(loaders[t]);
  if (REVIEW) guard(loadLedger);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
async function loadLedger() {
  const c = await op("prisma_counts", { review_id: REVIEW });
  const i = c.identification, s = c.screening, r = c.retrieval, e = c.eligibility, inc = c.included;
  const conf = await op("conflicts", { review_id: REVIEW, stage: "title_abstract" }).catch(() => []);
  const hint = (id, txt, hot) => { const el = $("#" + id); if (el) { el.textContent = txt; el.classList.toggle("hot", !!hot); } };
  hint("nh-search", i.records_total ? fmt(i.records_total) : "");
  hint("nh-records", c.dedup.pending_uncertain_matches ? `${c.dedup.pending_uncertain_matches} to check` : (s.records_screened ? fmt(s.records_screened) : ""), c.dedup.pending_uncertain_matches);
  hint("nh-screening", s.awaiting_screening ? `${s.awaiting_screening} left` : (s.records_screened ? "done" : ""), s.awaiting_screening);
  hint("nh-fulltext", r.reports_sought ? `${r.reports_sought - r.reports_not_retrieved - r.retrieval_pending}/${r.reports_sought}` : "");
  hint("nh-prisma", inc.reports ? `${inc.reports} incl.` : "");
  hint("nh-adjudication", conf.length ? `${conf.length}` : "", conf.length);
  const ok = c.checks.slice(0, -1).every((k) => k.ok);
  $("#miniLedger").innerHTML = `<div class="ml-title">Review ledger</div>
    <div class="ml-row"><span>Records found</span><b>${fmt(i.records_total)}</b></div>
    <div class="ml-row"><span>Duplicates removed</span><b>${fmt(i.duplicates_removed)}</b></div>
    <div class="ml-row"><span>Screened</span><b>${fmt(s.records_screened - s.awaiting_screening)}/${fmt(s.records_screened)}</b></div>
    <div class="ml-row"><span>Full texts</span><b>${fmt(e.reports_assessed)}</b></div>
    <div class="ml-row"><span>Included studies</span><b>${fmt(inc.studies)}</b></div>
    <div class="ml-row small"><span>PRISMA totals</span><b style="color:var(--${ok ? "ok" : "bad"})">${ok ? "reconcile ✓" : "mismatch"}</b></div>`;
}
async function loadReviews(select) {
  const rs = await op("list_reviews");
  const sel = $("#reviewSel");
  sel.innerHTML = rs.map((r) => `<option value="${r.review_id}">${esc(r.title)} (${r.reports} reports)</option>`).join("") || `<option value="">No reviews yet</option>`;
  const want = select || localStorage.getItem("ed_review");
  if (want && rs.some((r) => String(r.review_id) === String(want))) sel.value = want;
  REVIEW = sel.value ? Number(sel.value) : null;
}
$("#reviewSel").onchange = () => { REVIEW = Number($("#reviewSel").value); localStorage.setItem("ed_review", REVIEW); RUN = null; showTab(localStorage.getItem("ed_tab") || "search"); };
$("#newReviewBtn").onclick = () => guard(async () => {
  const title = prompt("Review title"); if (!title) return;
  const type = prompt("Evidence type: human, animal or mixed", "mixed") || "mixed";
  const r = await op("create_review", { title, evidence_type: type });
  await loadReviews(r.review_id); localStorage.setItem("ed_review", r.review_id); showTab("protocol");
});

// ------------------------------------------------------------------ protocol
const ELEMENTS = ["population", "intervention", "comparator", "outcomes", "study_design"];
function critRow(c) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="c-id" value="${esc(c.id)}" size="3"></td>
   <td><select class="c-el">${ELEMENTS.map((e) => `<option ${e === c.element ? "selected" : ""}>${e}</option>`).join("")}</select></td>
   <td><input class="c-label" value="${esc(c.label || "")}"></td>
   <td><select class="c-status">${["required", "optional", "not_applicable"].map((s) => `<option ${s === c.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
   <td><textarea class="c-inc" rows="2">${esc(c.include || "")}</textarea></td><td><textarea class="c-exc" rows="2">${esc(c.exclude || "")}</textarea></td>
   <td><textarea class="c-terms" rows="2">${esc((c.search_terms || []).join("\n"))}</textarea></td>
   <td><input type="checkbox" class="c-ins" ${c.in_search ? "checked" : ""}></td><td><button class="ghost danger c-del">✕</button></td>`;
  $(".c-del", tr).onclick = () => tr.remove();
  return tr;
}
async function loadProtocol() {
  const p = await op("get_protocol", { review_id: REVIEW });
  const vers = await op("list_protocol_versions", { review_id: REVIEW });
  PROTO = p;
  const pic = p ? p.picos : await op("protocol_template", { evidence_type: "mixed" });
  $("#protoVersions").textContent = p ? `Current: v${p.version} (${vers.length} version${vers.length > 1 ? "s" : ""}) · ${vers.map((v) => `v${v.version} ${v.change_note || ""}`).join(" · ")}` : "No protocol saved yet (showing template)";
  $("#pQuestion").value = pic.question || ""; $("#pType").value = pic.evidence_type || "mixed";
  $("#pRevTA").value = (pic.reviewers_required || {}).title_abstract || 2; $("#pRevFT").value = (pic.reviewers_required || {}).full_text || 2;
  $("#critBody").innerHTML = ""; pic.criteria.forEach((c) => $("#critBody").append(critRow(c)));
  $("#pReasons").value = (pic.exclusion_reasons || []).join("\n");
}
function readProtocol() {
  return { question: $("#pQuestion").value, evidence_type: $("#pType").value,
    reviewers_required: { title_abstract: Number($("#pRevTA").value), full_text: Number($("#pRevFT").value) },
    criteria: $$("#critBody tr").map((tr) => ({ id: $(".c-id", tr).value.trim(), element: $(".c-el", tr).value, label: $(".c-label", tr).value, status: $(".c-status", tr).value,
      include: $(".c-inc", tr).value, exclude: $(".c-exc", tr).value, search_terms: $(".c-terms", tr).value.split("\n").map((s) => s.trim()).filter(Boolean), in_search: $(".c-ins", tr).checked })),
    exclusion_reasons: $("#pReasons").value.split("\n").map((s) => s.trim()).filter(Boolean) };
}
$("#addCrit").onclick = () => $("#critBody").append(critRow({ id: "X" + ($$("#critBody tr").length + 1), element: "outcomes", status: "optional", in_search: false }));
$("#saveProto").onclick = () => guard(async () => { const r = await op("save_protocol", { review_id: REVIEW, picos: readProtocol(), note: $("#pNote").value || null }); $("#protoMsg").textContent = `Saved protocol v${r.version}`; loadProtocol(); });
$("#draftQuery").onclick = () => guard(async () => {
  const d = await op("draft_query_from_protocol", { review_id: REVIEW });
  $("#protoMsg").textContent = `Draft (criteria in search: ${d.criteria_in_search.join(", ")}). Left out of the search but applied at screening:\n` + d.left_out.map((x) => `  ${x.criterion} (${x.element}): ${x.why}`).join("\n");
  $("#q").value = d.query; renderHL(); showTab("search"); validateNow();
});

// ------------------------------------------------------------------ query editor
const q = $("#q"), hl = $("#hl");
const DEFAULT_Q = `SOURCES ALL\n\nTITLE_ABSTRACT(\n  (ANGPTL3 OR "angiopoietin-like protein 3")\n  AND\n  (CRISPR OR "genome editing" OR "base editing" OR "prime editing")\n)\nOR TITLE_ABSTRACT(CTX310 OR "CTX-310")\n`;
let lastErr = null;
function renderHL() {
  const t = q.value;
  let out = "", i = 0;
  const err = lastErr;
  const re = /(#[^\n]*)|("[^"\n]*"?)|\b(AND|OR|NOT)\b|\b(TITLE_ABSTRACT|TITLE|ABSTRACT|ALL_FIELDS|AUTHOR|JOURNAL|KEYWORDS|MESH|EMTREE|NEAR\/\d+)(?=\()|^(\s*(?:SOURCES|PUBDATE)\b)/gm;
  let m;
  const wrapErr = (s, start) => {
    if (!err) return esc(s);
    const a = Math.max(err.start, start), b = Math.min(err.end, start + s.length);
    if (a >= b) return esc(s);
    return esc(s.slice(0, a - start)) + `<span class="hl-err">${esc(s.slice(a - start, b - start))}</span>` + esc(s.slice(b - start));
  };
  while ((m = re.exec(t))) {
    out += wrapErr(t.slice(i, m.index), i);
    const cls = m[1] ? "hl-com" : m[2] ? "hl-str" : m[3] ? "hl-op" : m[4] ? "hl-field" : "hl-dir";
    out += `<span class="${cls}">${wrapErr(m[0], m.index)}</span>`;
    i = m.index + m[0].length;
  }
  out += wrapErr(t.slice(i), i);
  if (err && err.start >= t.length) out += `<span class="hl-err"> </span>`;
  hl.innerHTML = out + "\n";
}
q.addEventListener("scroll", () => { hl.scrollTop = q.scrollTop; });
let vTimer = null, acTimer = null, acItems = [], acSel = 0, acRange = null;
q.addEventListener("input", () => { renderHL(); clearTimeout(vTimer); vTimer = setTimeout(validateNow, 300); clearTimeout(acTimer); acTimer = setTimeout(autocomplete, 180); localStorage.setItem("ed_q", q.value); });
async function validateNow() {
  const v = await op("validate_query", { text: q.value });
  const st = $("#qStatus");
  if (!v.valid) { lastErr = v.errors[0]; st.className = "qstatus err"; st.textContent = v.formatted_error; }
  else { lastErr = null; st.className = "qstatus ok"; st.innerHTML = `✓ valid · ${v.clauses} clauses · interpreted as: ${esc(v.canonical)}` + v.warnings.map((w) => `<div class="warn">⚠ line ${w.line}: ${esc(w.message)}</div>`).join(""); }
  renderHL();
  return v;
}
function caretXY() {
  const div = document.createElement("div"); const cs = getComputedStyle(q);
  for (const p of ["fontFamily", "fontSize", "lineHeight", "padding", "border", "whiteSpace", "wordWrap", "width", "letterSpacing", "tabSize"]) div.style[p] = cs[p];
  Object.assign(div.style, { position: "absolute", visibility: "hidden", overflowWrap: "break-word" });
  div.textContent = q.value.slice(0, q.selectionStart); const span = document.createElement("span"); span.textContent = "​"; div.append(span);
  document.body.append(div); const r = { x: span.offsetLeft, y: span.offsetTop + span.offsetHeight - q.scrollTop }; div.remove(); return r;
}
async function autocomplete() {
  if (document.activeElement !== q) return;
  const res = await op("complete_query", { text: q.value, pos: q.selectionStart, review_id: REVIEW, mesh: true }).catch(() => null);
  const ac = $("#ac");
  if (!res || !res.items.length || (res.to - res.from === 0 && !/[(\s]$/.test(q.value.slice(0, q.selectionStart)))) { ac.classList.add("hidden"); return; }
  acItems = res.items; acSel = 0; acRange = [res.from, res.to];
  ac.innerHTML = acItems.map((it, i) => `<li data-i="${i}" class="${i === 0 ? "sel" : ""}"><span>${esc(it.label)}</span><small>${esc(it.kind)} · ${esc(it.detail || "")}</small></li>`).join("");
  const xy = caretXY(); ac.style.left = Math.min(xy.x + 10, q.clientWidth - 260) + "px"; ac.style.top = xy.y + 14 + "px"; ac.classList.remove("hidden");
  $$("li", ac).forEach((li) => li.onmousedown = (e) => { e.preventDefault(); acAccept(Number(li.dataset.i)); });
}
function acAccept(i) {
  const it = acItems[i]; if (!it) return;
  let label = it.label;
  if (it.kind === "mesh") label = label + '")';
  q.setRangeText(label, acRange[0], acRange[1], "end"); $("#ac").classList.add("hidden"); renderHL(); validateNow(); q.focus();
}
q.addEventListener("keydown", (e) => {
  const ac = $("#ac"); if (ac.classList.contains("hidden")) { if (e.key === "Tab") { e.preventDefault(); q.setRangeText("  ", q.selectionStart, q.selectionEnd, "end"); renderHL(); } return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); acSel = (acSel + (e.key === "ArrowDown" ? 1 : -1) + acItems.length) % acItems.length; $$("li", ac).forEach((li, i) => li.classList.toggle("sel", i === acSel)); }
  else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); acAccept(acSel); }
  else if (e.key === "Escape") ac.classList.add("hidden");
});
q.addEventListener("blur", () => setTimeout(() => $("#ac").classList.add("hidden"), 150));
$("#grammarLink").onclick = (e) => { e.preventDefault(); guard(async () => { const g = await op("grammar"); $("#dlgBody").innerHTML = `<pre>${esc(g)}</pre>`; $("#dlg").showModal(); }); };

function noteList(notes) {
  if (!notes || !notes.length) return `<div class="muted small">No semantic differences.</div>`;
  return `<ul class="notes">${notes.map((n) => `<li class="sev-${n.severity}"><b>${n.severity}</b> ${n.clause ? `<code>${esc(n.clause)}</code>: ` : ""}${esc(n.message)}</li>`).join("")}</ul>`;
}
$("#previewBtn").onclick = () => guard(async () => {
  const v = await validateNow(); if (!v.valid) return;
  const p = await op("preview_query", { text: q.value });
  $("#preview").innerHTML = p.sources.map((s) => `<div class="card" data-src="${s.source}"><h4><span>${esc(s.platform)}</span><span>${badge(s.status)} ${badge(s.execution)}</span></h4>
    <div class="small muted">connector: ${badge(s.connector.status)} ${esc(s.connector.detail)} ${s.verified_live ? "· verified live" : "· not verified live"}</div>
    <div class="small">fields: ${esc(Object.entries(s.fields).map(([k, v]) => `${k}→${v}`).join(", "))}</div>
    <pre>${esc(s.native)}</pre>${noteList(s.notes)}
    ${s.execution === "awaiting_approval" ? `<label class="inline"><input type="checkbox" class="approve" value="${s.source}"> Approve this approximation for the run</label>` : ""}
    ${s.execution === "manual" || s.execution === "disconnected" ? `<div class="small sev-approximation">Not run automatically. Copy the native query, run it on ${esc(s.platform)}, and import the export (Search › Import).</div>` : ""}
    <button class="ghost small copy">Copy native query</button></div>`).join("");
  $$("#preview .copy").forEach((b) => b.onclick = () => { navigator.clipboard.writeText($("pre", b.parentElement).textContent); toast("Copied"); });
});
$("#runBtn").onclick = () => guard(async () => {
  const v = await validateNow(); if (!v.valid) return;
  if (!REVIEW) return toast("Create a review first", true);
  let approve = $$("#preview .approve:checked").map((c) => c.value);
  if (!$("#preview").children.length) {
    // no preview shown yet: show translations and ask once about approximations instead of silently skipping them
    const p = await op("preview_query", { text: q.value });
    const pend = p.sources.filter((s) => s.execution === "awaiting_approval");
    if (pend.length && confirm(`These databases need an approximate translation:\n\n${pend.map((s) => "• " + s.platform + ": " + s.notes.filter((n) => n.severity === "approximation").map((n) => n.message).join(" ")).join("\n")}\n\nApprove them for this search? (Cancel = run only exact translations; you can approve later from the results.)`)) approve = pend.map((s) => s.source);
  }
  $("#runBtn").classList.add("busy"); window.dispatchEvent(new Event("cosmos:warp"));
  const r = await op("start_search", { review_id: REVIEW, query: q.value, approve, max_records: Number($("#maxRec").value), actor: "ui" }).finally(() => $("#runBtn").classList.remove("busy"));
  RUN = r.run_id; lastNum.clear(); pollRun();
});
const SRC_STATE = { complete: ["b-ok", "complete"], complete_with_alerts: ["b-warn", "complete · alerts"], running: ["b-info", "searching…"], queued: ["b-info", "queued"],
  awaiting_approval: ["b-warn", "needs approval"], disconnected: ["b-bad", "not connected"], manual_required: ["b-warn", "manual + import"], failed: ["b-bad", "failed"],
  access_denied: ["b-bad", "access denied"], partial: ["b-warn", "incomplete"], skipped: ["b-mute", "skipped"], imported: ["b-ok", "imported"] };
async function pollRun() {
  clearTimeout(pollTimer); if (!RUN) return;
  const st = await op("search_status", { run_id: RUN });
  const d = st.dashboard;
  const active = st.active || st.sources.some((s) => ["queued", "running"].includes(s.status));
  $("#runArea").classList.remove("hidden");
  $("#runId").textContent = `run #${RUN} · ${st.status.replaceAll("_", " ")}`;
  $("#runProgress").classList.toggle("done", !active);
  const ran = st.sources.filter((s) => s.retrieved);
  const totalRec = ran.reduce((a, s) => a + (s.retrieved || 0), 0);
  const dup = Math.max(0, totalRec - d.unique_reports_total);
  $("#statTiles").innerHTML = `
    <div class="tile accent"><div class="k">Records retrieved</div><div class="v" data-key="t-rec" data-count="${totalRec}">${fmt(totalRec)}</div><div class="s">from ${ran.length} database${ran.length === 1 ? "" : "s"}</div></div>
    <div class="tile"><div class="k">Unique reports</div><div class="v" data-key="t-uni" data-count="${d.unique_reports_total}">${fmt(d.unique_reports_total)}</div><div class="s">${fmt(st.new_reports_this_run)} new to this review</div></div>
    <div class="tile"><div class="k">Duplicates removed</div><div class="v" data-key="t-dup" data-count="${dup}">${fmt(dup)}</div><div class="s">by DOI / PMID / PMCID</div></div>
    <div class="tile"><div class="k">Needs attention</div><div class="v">${st.sources.filter((s) => !["complete", "imported", "skipped"].includes(s.status)).length}</div><div class="s">${st.sources.filter((s) => ["disconnected", "manual_required"].includes(s.status)).length} not connected · ${st.sources.filter((s) => s.status === "awaiting_approval").length} awaiting approval</div></div>`;
  const maxRet = Math.max(1, ...st.sources.map((s) => s.retrieved || 0));
  $("#dashboard").innerHTML = `<div class="src-grid">` + st.sources.map((s, i) => {
    const r = d.sources[i]; const [cls, label] = SRC_STATE[s.status] || ["b-mute", s.status];
    const uniqW = (r.unique_reports / maxRet) * 100, ovW = (r.overlap_reports / maxRet) * 100;
    const big = s.reported_hits === null || s.reported_hits === undefined ? (s.status === "disconnected" ? "—" : "…") : `<span data-key="src-${s.source}" data-count="${s.retrieved}">${fmt(s.retrieved)}</span>`;
    return `<div class="src ${s.retrieved || s.status === "running" ? "" : "dim"}"><div class="src-head"><div><div class="src-name">${esc(s.platform.replace(/ \(.*\)$/, ""))}</div><div class="small muted">${esc(s.fidelity)} translation${s.approved_by ? " · approved" : ""}</div></div><span class="badge ${cls}">${label}</span></div>
      <div class="src-big">${big} <small>${s.reported_hits !== null && s.reported_hits !== undefined ? `of ${fmt(s.reported_hits)} hits` : ""}</small></div>
      ${s.zero_results ? `<div class="small" style="color:var(--info)">Search succeeded with 0 hits (not an error).</div>` : ""}
      <div class="bar"><i class="u" style="width:${uniqW}%"></i><i class="o" style="width:${ovW}%"></i></div>
      <div class="legend"><span><i style="background:#8b5cf6"></i>only here <b>${r.unique_reports}</b></span><span><i style="background:rgba(56,189,248,.7)"></i>also elsewhere <b>${r.overlap_reports}</b></span>${s.retries ? `<span>retries <b>${s.retries}</b></span>` : ""}</div>
      ${s.error || s.incomplete_reason ? `<div class="src-msg" style="color:var(--${s.status === "disconnected" ? "muted" : "warn"})">${esc(s.incomplete_reason || "")} ${esc(s.error || "")}</div>` : ""}
      <div class="src-actions">${s.status === "awaiting_approval" ? `<button class="primary compact ap" data-s="${s.source}">Approve &amp; run</button><button class="ghost compact sk" data-s="${s.source}">Skip</button>` : ""}
      ${["failed", "partial"].includes(s.status) ? `<button class="compact rs">Resume</button>` : ""}
      ${["disconnected", "manual_required"].includes(s.status) ? `<button class="ghost compact cp" data-i="${i}">Copy query for manual run</button><button class="ghost compact" onclick="showTab('settings')">Add key</button>` : ""}
      <button class="ghost compact nq" data-i="${i}">Native query</button></div></div>`; }).join("") + `</div>`;
  $$("#dashboard .ap").forEach((b) => b.onclick = () => guard(async () => { await op("approve_source", { run_id: RUN, source: b.dataset.s, actor: "ui" }); pollRun(); }));
  $$("#dashboard .sk").forEach((b) => b.onclick = () => guard(async () => { await op("skip_source", { run_id: RUN, source: b.dataset.s, actor: "ui" }); pollRun(); }));
  $$("#dashboard .rs").forEach((b) => b.onclick = () => guard(async () => { await op("resume_search", { run_id: RUN }); pollRun(); }));
  $$("#dashboard .cp").forEach((b) => b.onclick = () => { navigator.clipboard.writeText(st.sources[b.dataset.i].native_query || ""); toast("Native query copied — run it on the platform, then use Import below"); });
  $$("#dashboard .nq").forEach((b) => b.onclick = () => { const s = st.sources[b.dataset.i]; $("#dlgBody").innerHTML = `<h3>${esc(s.platform)}</h3><pre>${esc(s.native_query)}</pre>${noteList(s.notes)}${s.platform_notes ? `<p class="small muted">${esc(s.platform_notes)}</p>` : ""}`; $("#dlg").showModal(); });
  $("#nextSteps").innerHTML = active ? "" : `<div class="next"><b>What next</b><div class="next-steps">
    <button onclick="showTab('records')"><b>Check duplicates</b><span>Resolve uncertain matches</span></button>
    <button onclick="showTab('screening')"><b>Screen records</b><span>Title / abstract against PICOS</span></button>
    <button onclick="showTab('fulltext')"><b>Fetch full texts</b><span>Open-access PDFs and XML</span></button>
    <button onclick="showTab('prisma')"><b>Export packet</b><span>One PDF for your AI agent</span></button></div></div>`;
  countUp($("#runArea"));
  if (active) pollTimer = setTimeout(pollRun, 1500);
  else { loadRuns(); loadLedger(); }
}
async function loadChips() {
  const ss = await op("list_sources");
  $("#sourceChips").innerHTML = ss.filter((s) => s.role === "search").map((s) => {
    const cls = s.status === "public" ? "on" : s.status === "configured" ? "key" : s.name === "embase" ? "imp" : "off";
    const tip = s.status === "disconnected" ? "Not connected — add a key in Databases & keys, or import an export" : s.detail || "";
    return `<span class="chip ${cls}" title="${esc(tip)}"><i></i>${esc(s.platform.replace(/ \(.*\)$/, "").replace(/ (Search )?API.*$/, "").replace(" REST", "").replace(" Works", "").replace(" syntax", ""))}</span>`;
  }).join("") + `<span class="chip imp" title="RIS / CSV / XML exports from Ovid, CENTRAL, CINAHL…"><i></i>+ imports</span>`;
}
async function loadSearchTab() {
  if (!q.value) q.value = localStorage.getItem("ed_q") || DEFAULT_Q; renderHL(); validateNow(); guard(loadChips);
  const sq = await op("list_saved_queries", { review_id: REVIEW });
  $("#savedSel").innerHTML = `<option value="">Saved queries…</option>` + sq.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (updated ${s.updated_at.slice(0, 10)})</option>`).join("");
  $("#savedSel").onchange = () => { const s = sq.find((x) => x.name === $("#savedSel").value); if (s) { q.value = s.query_text; $("#savedName").value = s.name; renderHL(); validateNow(); } };
  await loadRuns();
  if (!RUN) { const runs = await op("list_runs", { review_id: REVIEW }); if (runs.length) RUN = runs[0].run_id; }
  pollRun();
}
async function loadRuns() {
  const runs = await op("list_runs", { review_id: REVIEW });
  $("#runsList").innerHTML = `<table><tr><th>Run</th><th>Status</th><th>Started</th><th>Saved query</th><th>Query</th><th></th></tr>` + runs.map((r) => `<tr><td>#${r.run_id}</td><td>${badge(r.status)}</td><td>${esc(r.started_at)}</td><td>${esc(r.saved_query || "")}</td><td class="mono small">${esc(r.query)}</td><td><button class="ghost" data-r="${r.run_id}">Show</button> <button class="ghost df" data-r="${r.run_id}">What's new</button></td></tr>`).join("") + `</table>`;
  $$("#runsList button[data-r]:not(.df)").forEach((b) => b.onclick = () => { RUN = Number(b.dataset.r); pollRun(); });
  $$("#runsList .df").forEach((b) => b.onclick = () => guard(async () => showDiff(await op("run_diff", { run_id: Number(b.dataset.r) }))));
}
function showDiff(d) { $("#diff").innerHTML = `<div class="card"><h4>Run #${d.run_id} vs ${esc(d.compared_to)}</h4><b>${d.new_reports.length} newly found</b><ul>${d.new_reports.map((x) => `<li>${esc(x.report_id)} ${esc(x.title)}</li>`).join("")}</ul>${d.no_longer_found.length ? `<b>${d.no_longer_found.length} no longer returned</b><ul>${d.no_longer_found.map((x) => `<li>${esc(x.report_id)} ${esc(x.title)}</li>`).join("")}</ul>` : ""}</div>`; }
$("#saveQueryBtn").onclick = () => guard(async () => { const n = $("#savedName").value.trim(); if (!n) return toast("Name the query", true); await op("save_query", { review_id: REVIEW, name: n, text: q.value }); toast("Saved"); loadSearchTab(); });
$("#rerunBtn").onclick = () => guard(async () => {
  const n = $("#savedName").value.trim() || $("#savedSel").value; if (!n) return toast("Pick a saved query", true);
  toast("Running saved query…");
  const approve = $$("#preview .approve:checked").map((c) => c.value);
  const r = await op("rerun_saved_query", { review_id: REVIEW, name: n, approve, actor: "ui", background: false });
  RUN = r.run_id; lastNum.clear(); pollRun(); showDiff(r.diff);
});
$("#importForm").onsubmit = (e) => { e.preventDefault(); guard(async () => {
  const fd = new FormData(e.target);
  const r = await fetch(`/api/reviews/${REVIEW}/import`, { method: "POST", headers: { "X-EvidenceDesk-Token": window.ED_TOKEN }, body: fd }).then((r) => r.json());
  if (!r.ok) throw new Error(r.error);
  $("#importMsg").textContent = `Imported ${r.result.records} records (${r.result.new_records} new) as ${r.result.format}; file SHA-256 ${r.result.sha256}`; loadRuns();
}); };

// ------------------------------------------------------------------ records & dedup
async function loadRecords() {
  const reps = await op("list_reports", { review_id: REVIEW, q: $("#repSearch").value || null, limit: 500 });
  $("#reportsList").innerHTML = reps.map((r) => `<div class="item" data-id="${r.id}"><div class="t">${esc(r.report_id)} · ${esc(r.title)}</div><div class="m">${esc(r.year || "")} · ${esc(r.journal || "")} · ${badge(r.report_kind)} · ${esc(r.sources.join(", "))} · DOI ${esc(r.doi || "—")} · PMID ${esc(r.pmid || "—")} · ${badge(r.ft_status)}</div></div>`).join("") || `<p class="muted" style="padding:10px">No reports yet.</p>`;
  $$("#reportsList .item").forEach((it) => it.onclick = () => openReport(Number(it.dataset.id)));
  const cands = await op("dedup_candidates", { review_id: REVIEW });
  $("#dedupCount").textContent = cands.length ? cands.length : "";
  const side = (x) => `<div><b>RPT-${x.id}</b> ${badge(x.report_kind)}<div>${esc(x.title)}</div><div class="small muted">${esc(x.year)} · ${esc(x.journal || "")} · DOI ${esc(x.doi || "—")} · PMID ${esc(x.pmid || "—")}${x.merged_into ? ` · merged into RPT-${x.merged_into}` : ""}</div></div>`;
  $("#dedupList").innerHTML = cands.map((c) => `<div class="card"><h4><span>Similarity ${Math.round(c.score || 0)} — ${esc(c.reason)}</span></h4><div class="pair">${side(c.a)}${side(c.b)}</div>
    <div class="row"><button class="primary" data-c="${c.id}" data-d="merge">Same report: merge</button><button data-c="${c.id}" data-d="same_study">Different reports, same study: link</button><button class="ghost" data-c="${c.id}" data-d="not_duplicate">Not duplicates</button></div></div>`).join("") || `<p class="muted">No uncertain matches pending.</p>`;
  $$("#dedupList button[data-c]").forEach((b) => b.onclick = () => guard(async () => { await op("decide_candidate", { review_id: REVIEW, candidate_id: Number(b.dataset.c), decision: b.dataset.d, actor: "ui" }); loadRecords(); }));
  const evs = await op("merge_events", { review_id: REVIEW });
  $("#mergeEvents").innerHTML = `<table><tr><th>#</th><th>Kind</th><th>Target</th><th>Source</th><th>Method</th><th>Actor</th><th>When</th><th></th></tr>` + evs.map((e) => `<tr><td>${e.id}</td><td>${e.kind}</td><td>RPT-${e.target_report}</td><td>RPT-${e.source_report}</td><td>${esc(e.method)}</td><td>${esc(e.actor)}</td><td>${esc(e.created_at)}</td><td>${e.undone_at ? `undone ${esc(e.undone_at)}` : `<button class="ghost" data-e="${e.id}">Undo</button>`}</td></tr>`).join("") + `</table>`;
  $$("#mergeEvents button[data-e]").forEach((b) => b.onclick = () => guard(async () => { await op("undo_merge", { review_id: REVIEW, event_id: Number(b.dataset.e), actor: "ui" }); loadRecords(); }));
}
$("#repSearch").oninput = () => { clearTimeout(vTimer); vTimer = setTimeout(() => guard(loadRecords), 300); };
async function openReport(id) {
  const r = await op("get_report", { report_id: id });
  $("#dlgBody").innerHTML = `<h3>${esc(r.report_id)} · ${esc(r.title)}</h3><div class="small muted">${esc(r.authors.slice(0, 8).join(", "))} · ${esc(r.year)} · ${esc(r.journal)} · DOI ${esc(r.doi)} · PMID ${esc(r.pmid)} · PMCID ${esc(r.pmcid)} · NCT ${esc(r.nct_ids.join(", "))}</div>
   <p>${esc(r.abstract || "No abstract")}</p><h4>Source records (all preserved)</h4><table><tr><th>Record</th><th>Source</th><th>Source ID</th><th>Link</th><th></th></tr>${r.records.map((x) => `<tr><td>REC-${x.id}</td><td>${esc(x.source)}</td><td>${esc(x.source_record_id)}</td><td>${esc(x.method)} ${x.score ? Math.round(x.score) : ""}</td><td>${r.records.length > 1 ? `<button class="ghost sp" data-rec="${x.id}">Split off</button>` : ""}</td></tr>`).join("")}</table>
   ${r.study ? `<p>Study STU-${r.study.study_id}: ${r.study.reports.map((x) => `RPT-${x.report_id} (${esc(x.role)})`).join(", ")}</p>` : ""}`;
  $$("#dlgBody .sp").forEach((b) => b.onclick = () => guard(async () => { await op("split_record", { review_id: REVIEW, record_id: Number(b.dataset.rec), actor: "ui" }); toast("Split; undo from merge history"); $("#dlg").close(); loadRecords(); }));
  $("#dlg").showModal();
}

// ------------------------------------------------------------------ studies
async function loadStudies() {
  const sugg = await op("suggest_study_links", { review_id: REVIEW });
  $("#studySugg").innerHTML = sugg.map((s) => `<div class="card"><b>${esc(s.reason)}</b><div class="pair"><div>RPT-${s.a.id} ${badge(s.a.report_kind)} ${esc(s.a.title)}</div><div>RPT-${s.b.id} ${badge(s.b.report_kind)} ${esc(s.b.title)}</div></div><div class="row"><button class="primary" data-s="${s.id}" data-a="1">Link as same study</button><button class="ghost" data-s="${s.id}" data-a="0">Reject</button></div></div>`).join("") || `<p class="muted">No pending suggestions.</p>`;
  $$("#studySugg button").forEach((b) => b.onclick = () => guard(async () => { await op("decide_study_link", { review_id: REVIEW, suggestion_id: Number(b.dataset.s), accept: b.dataset.a === "1", actor: "ui" }); loadStudies(); }));
  const st = await op("list_studies", { review_id: REVIEW });
  $("#studiesList").innerHTML = st.map((s) => `<div class="card"><h4>${esc(s.study_id)} · ${esc(s.label)}</h4><ul>${s.reports.map((r) => `<li>RPT-${r.report_id} [${esc(r.role)}] ${esc(r.title)} <span class="muted small">${esc(r.evidence || "")}</span></li>`).join("")}</ul>${s.cohort_links.map((c) => `<div class="small sev-approximation">⚑ ${esc(c.kind)} with STU-${c.study_a === s.id ? c.study_b : c.study_a}: ${esc(c.notes || "")}</div>`).join("")}</div>`).join("") || `<p class="muted">No studies yet.</p>`;
}
$("#createStudy").onclick = () => guard(async () => { await op("create_study", { review_id: REVIEW, label: $("#newStudyLabel").value, report_ids: $("#newStudyReports").value.split(/[,\s]+/).filter(Boolean), actor: "ui" }); loadStudies(); });
$("#addCL").onclick = () => guard(async () => { await op("add_cohort_link", { review_id: REVIEW, study_a: $("#clA").value, study_b: $("#clB").value, kind: $("#clKind").value, notes: $("#clNotes").value, actor: "ui" }); loadStudies(); });

// ------------------------------------------------------------------ screening
$("#reviewer").value = localStorage.getItem("ed_reviewer") || "";
$("#reviewer").onchange = () => localStorage.setItem("ed_reviewer", $("#reviewer").value);
["stage", "sFilter"].forEach((id) => $("#" + id).onchange = () => guard(loadQueue));
$("#kwRank").onclick = () => guard(async () => { const r = await op("keyword_prioritize", { review_id: REVIEW, stage: $("#stage").value }); toast(`Ranked ${r.ranked} reports (priority only)`); loadQueue(); });
async function loadQueue() {
  PROTO = await op("get_protocol", { review_id: REVIEW });
  if (!PROTO) { $("#sQueue").innerHTML = `<p class="muted" style="padding:10px">Save a PICOS protocol first.</p>`; return; }
  const items = await op("screening_queue", { review_id: REVIEW, stage: $("#stage").value, filter: $("#sFilter").value, reviewer: $("#reviewer").value || null, limit: 1000 });
  $("#sQueue").innerHTML = items.map((x) => `<div class="item ${x.report_id === SEL_REPORT ? "sel" : ""}" data-id="${x.report_id}"><div class="t">RPT-${x.report_id} · ${esc(x.title)}</div>
    <div class="m">${badge(x.resolution.final)} ${esc(x.resolution.basis)} · ${badge(x.report_kind)} ${x.priority !== null && x.priority !== undefined ? `· priority ${x.priority} (${esc(x.priority_by)})` : ""} ${x.agent_proposals.length ? `· agent: ${x.agent_proposals.join("/")}` : ""} ${x.reviewer_decisions.map((d) => `· ${esc(d.actor)}: ${d.decision}`).join(" ")}</div></div>`).join("") || `<p class="muted" style="padding:10px">Nothing in this view.</p>`;
  $$("#sQueue .item").forEach((it) => it.onclick = () => { SEL_REPORT = Number(it.dataset.id); $$("#sQueue .item").forEach((i) => i.classList.toggle("sel", i === it)); guard(() => openScreen(SEL_REPORT)); });
}
function decisionHTML(d) {
  const tag = { agent: "PROPOSED (agent)", reviewer: "CONFIRMED (reviewer)", adjudicator: "ADJUDICATED" }[d.actor_type];
  const ver = d.passage_verified === 1 ? `<span class="badge b-ok">passage found in source</span>` : d.passage_verified === 0 ? `<span class="badge b-bad">passage NOT found in source</span>` : "";
  return `<div class="${d.actor_type === "agent" ? "prop" : "decbox"}"><div><b>${tag}</b> · ${esc(d.actor)} · protocol v${d.protocol_version} ${PROTO && d.protocol_version !== PROTO.version ? `<span class="badge b-warn">older protocol</span>` : ""} · ${badge(d.decision)} ${d.criterion_id ? `criterion <b>${esc(d.criterion_id)}</b>` : ""} ${d.exclusion_reason ? `· ${esc(d.exclusion_reason)}` : ""} ${d.confidence !== null && d.confidence !== undefined ? `· confidence ${esc(d.confidence)}` : ""}</div>
   ${d.passage ? `<blockquote>${esc(d.passage)}</blockquote><div class="small muted">${esc(d.location || "no location")} ${ver}</div>` : ""}
   ${d.missing_info ? `<div class="small"><b>Missing / uncertain:</b> ${esc(d.missing_info)}</div>` : ""}${d.rationale ? `<div class="small">${esc(d.rationale)}</div>` : ""}
   ${d.actor_type === "agent" ? `<button class="ghost small adopt" data-id="${d.id}">Use as starting point</button>` : ""}</div>`;
}
async function openScreen(id) {
  const stage = $("#stage").value;
  const r = await op("get_report", { report_id: id, include_text: stage === "full_text" });
  const s = r.screening[stage];
  const crit = PROTO.picos.criteria;
  const ds = s.decisions;
  $("#sDetail").innerHTML = `<h3>${esc(r.report_id)} · ${esc(r.title)}</h3><div class="small muted">${esc(r.authors.slice(0, 6).join(", "))} · ${esc(r.year)} · ${esc(r.journal)} · ${badge(r.report_kind)} · DOI ${esc(r.doi || "—")} · PMID ${esc(r.pmid || "—")} · NCT ${esc(r.nct_ids.join(", ") || "—")}</div>
   <div class="row">${badge(s.resolution.final)} <span class="small">${esc(s.resolution.basis)}</span> · full text ${badge(r.ft_status)} ${r.ft_unavailable_reason ? `<span class="small muted">${esc(r.ft_unavailable_reason)}</span>` : ""}</div>
   <h4>Abstract</h4><p>${esc(r.abstract || "No abstract available")}</p>
   <details><summary>Protocol v${PROTO.version} criteria</summary><table>${crit.map((c) => `<tr><td><b>${esc(c.id)}</b></td><td>${esc(c.element)}</td><td>${badge(c.status)}</td><td>${esc(c.include)}</td><td class="muted">${esc(c.exclude)}</td></tr>`).join("")}</table></details>
   <h4>Agent proposals (stored separately; never counted)</h4>${ds.filter((d) => d.actor_type === "agent").map(decisionHTML).join("") || `<p class="muted small">None.</p>`}
   <h4>Reviewer / adjudicator decisions</h4>${ds.filter((d) => d.actor_type !== "agent").map(decisionHTML).join("") || `<p class="muted small">None yet.</p>`}
   <h4>Your decision</h4><form id="decForm" class="grid2">
     <label>Decision<select name="decision"><option>include</option><option>exclude</option><option>unclear</option></select></label>
     <label>Criterion that drove it<select name="criterion_id"><option value="">—</option>${crit.map((c) => `<option value="${esc(c.id)}">${esc(c.id)} ${esc(c.label)}</option>`).join("")}</select></label>
     <label>Exclusion reason ${stage === "full_text" ? "(required to exclude)" : ""}<select name="exclusion_reason"><option value="">—</option>${(PROTO.picos.exclusion_reasons || []).map((x) => `<option>${esc(x)}</option>`).join("")}</select></label>
     <label>Location of passage<input name="location" placeholder="Abstract / Methods ¶2 / Table 1 / p. 4"></label>
     <label style="grid-column: span 2">Supporting passage (verbatim)<textarea name="passage" rows="2"></textarea></label>
     <label style="grid-column: span 2">Missing information / uncertainty<input name="missing_info"></label>
     <label style="grid-column: span 2">Notes<input name="rationale"></label>
     <input type="hidden" name="based_on"><div><button class="primary">Record decision as ${esc($("#reviewer").value || "(set reviewer name)")}</button></div></form>
   ${r.segments ? `<h4>Full text (${r.segments.length} segments)</h4>${r.segments.slice(0, 600).map((g) => `<div class="seg"><span class="loc">[${esc(g.location)}]</span> ${esc(g.text)}</div>`).join("")}` : ""}
   ${stage === "full_text" ? `<div class="row"><button id="ftRetrieveOne">Try retrieval routes</button><input type="file" id="ftUploadOne"><button id="ftUnavail" class="ghost">Mark unavailable…</button></div>` : ""}`;
  $$("#sDetail .adopt").forEach((b) => b.onclick = () => { const d = ds.find((x) => x.id === Number(b.dataset.id)); const f = $("#decForm"); for (const k of ["decision", "criterion_id", "exclusion_reason", "location", "passage", "missing_info"]) f[k].value = d[k] || ""; f.based_on.value = d.id; });
  $("#decForm").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    const rev = $("#reviewer").value.trim(); if (!rev) throw new Error("Enter your reviewer name (top right)");
    const f = Object.fromEntries(new FormData(e.target)); for (const k in f) if (f[k] === "") f[k] = null;
    if (f.based_on) f.based_on = Number(f.based_on);
    const res = await op("reviewer_decision", { review_id: REVIEW, report_id: id, stage, reviewer: rev, ...f });
    toast(`Recorded · now ${res.resolution.final}` + (res.passage_verified === false ? " · WARNING: passage not found in source text" : ""), res.passage_verified === false);
    await loadQueue(); openScreen(id);
  }); };
  if ($("#ftRetrieveOne")) {
    $("#ftRetrieveOne").onclick = () => guard(async () => { toast("Retrieving…"); await op("retrieve_fulltext", { review_id: REVIEW, report_ids: [id] }); openScreen(id); });
    $("#ftUploadOne").onchange = (e) => guard(async () => { const fd = new FormData(); fd.append("file", e.target.files[0]); fd.append("actor", $("#reviewer").value || "ui"); await fetch(`/api/reports/${id}/upload`, { method: "POST", headers: { "X-EvidenceDesk-Token": window.ED_TOKEN }, body: fd }); openScreen(id); });
    $("#ftUnavail").onclick = () => guard(async () => { const why = prompt("Why is the full text unavailable? (e.g. requested from authors 2026-11-01, no reply)"); if (why) { await op("mark_fulltext_unavailable", { review_id: REVIEW, report_id: id, reason: why, actor: $("#reviewer").value || "ui" }); openScreen(id); } });
  }
}

// ------------------------------------------------------------------ adjudication
$("#adjStage").onchange = () => guard(loadAdj);
async function loadAdj() {
  PROTO = await op("get_protocol", { review_id: REVIEW });
  const cs = await op("conflicts", { review_id: REVIEW, stage: $("#adjStage").value });
  $("#adjList").innerHTML = cs.map((c) => `<div class="card"><h4>RPT-${c.report_id} · ${esc(c.title)}</h4>${c.decisions.map(decisionHTML).join("")}
    <div class="row"><select class="d"><option>include</option><option>exclude</option><option>unclear</option></select><select class="cr"><option value="">criterion</option>${PROTO.picos.criteria.map((x) => `<option>${esc(x.id)}</option>`).join("")}</select>
    <select class="er"><option value="">exclusion reason</option>${(PROTO.picos.exclusion_reasons || []).map((x) => `<option>${esc(x)}</option>`).join("")}</select><input class="ra" placeholder="rationale" size="40"><button class="primary" data-id="${c.report_id}">Adjudicate</button></div></div>`).join("") || `<p class="muted">No conflicts at this stage.</p>`;
  $$("#adjList button[data-id]").forEach((b) => b.onclick = () => guard(async () => {
    const card = b.closest(".card"); const adj = $("#adjudicator").value.trim(); if (!adj) throw new Error("Enter adjudicator name");
    await op("adjudicate", { review_id: REVIEW, report_id: Number(b.dataset.id), stage: $("#adjStage").value, decision: $(".d", card).value, adjudicator: adj, criterion_id: $(".cr", card).value || null, exclusion_reason: $(".er", card).value || null, rationale: $(".ra", card).value || null });
    loadAdj();
  }));
}

// ------------------------------------------------------------------ full text
async function loadFT() {
  const s = await op("fulltext_summary", { review_id: REVIEW });
  $("#ftSummary").innerHTML = `<div class="row">${Object.entries(s.by_status).map(([k, v]) => `${badge(k)} ${v}`).join(" · ")} · OCR ${s.ocr_available ? `<span class="badge b-ok">tesseract available</span>` : `<span class="badge b-bad">tesseract not installed</span>`}</div>` +
    (s.extraction_problems.length ? `<div class="card"><b>Extraction problems</b><ul>${s.extraction_problems.map((p) => `<li>RPT-${p.report_id}: ${badge(p.status)} ${esc(p.method)} ${esc(p.error)}</li>`).join("")}</ul></div>` : "");
  const reps = await op("list_reports", { review_id: REVIEW, limit: 1000 });
  const ft = await op("screening_queue", { review_id: REVIEW, stage: "full_text", limit: 2000 });
  const inFT = new Set(ft.map((x) => x.report_id));
  $("#ftList").innerHTML = reps.filter((r) => inFT.has(r.id) || r.ft_status !== "not_sought").map((r) => `<div class="item" data-id="${r.id}"><div class="t">${esc(r.report_id)} · ${esc(r.title)}</div><div class="m">${badge(r.ft_status)} · ${badge(r.report_kind)} · PMCID ${esc(r.pmcid || "—")} · DOI ${esc(r.doi || "—")}</div></div>`).join("") || `<p class="muted" style="padding:10px">No reports at full-text stage yet (include or mark unclear at title/abstract first).</p>`;
  $$("#ftList .item").forEach((it) => it.onclick = () => guard(async () => {
    const r = await op("get_report", { report_id: Number(it.dataset.id) });
    $("#dlgBody").innerHTML = `<h3>${esc(r.report_id)} · ${esc(r.title)}</h3><div>${badge(r.ft_status)} ${esc(r.ft_unavailable_reason || "")}</div><h4>Files</h4><ul>${r.files.map((f) => `<li><a href="/api/files/${f.id}?${tokenQS()}" target="_blank">${esc(f.original_name)}</a> (${esc(f.kind)}, ${esc(f.origin)}, ${f.size} bytes${f.license ? ", " + esc(f.license) : ""})</li>`).join("") || "<li>none</li>"}</ul>
      <h4>Text extraction</h4><ul>${r.extractions.map((e) => `<li>file ${e.file_id}: ${badge(e.status)} ${esc(e.method)} · ${e.chars} chars${e.pages ? ` · ${e.pages} pages` : ""}${e.ocr_pages ? ` · ${e.ocr_pages} OCR pages` : ""} ${esc(e.error || "")}</li>`).join("") || "<li>none</li>"}</ul>
      <h4>Every retrieval attempt</h4><table><tr><th>Route</th><th>Status</th><th>HTTP</th><th>Detail</th><th>When</th></tr>${r.retrieval_attempts.map((a) => `<tr><td>${esc(a.route)}</td><td>${badge(a.status)}</td><td>${esc(a.http_status || "")}</td><td class="small">${esc(a.detail || "")} <span class="muted">${esc(a.url || "")}</span></td><td class="small">${esc(a.attempted_at)}</td></tr>`).join("")}</table>`;
    $("#dlg").showModal();
  }));
}
$("#retrieveFT").onclick = () => guard(async () => {
  const ids = $("#retrieveIds").value.split(/[,\s]+/).filter(Boolean);
  toast("Retrieving full texts — this can take a while");
  const r = await op("retrieve_fulltext", { review_id: REVIEW, report_ids: ids.length ? ids : null, scope: "full_text_stage" });
  toast(`Processed ${r.reports} reports`); loadFT();
});

// ------------------------------------------------------------------ extraction & analysis
$("#exFilter").onchange = () => guard(loadExtraction);
$("#verifier").value = localStorage.getItem("ed_reviewer") || "";
async function loadExtraction() {
  const rows = await op("list_extraction", { review_id: REVIEW, status: $("#exFilter").value || null });
  $("#exList").innerHTML = `<table><tr><th>#</th><th>Status</th><th>Report</th><th>Experiment</th><th>Outcome</th><th>Time</th><th>Arm</th><th>n</th><th>Mean</th><th>SD</th><th>SE</th><th>Unit</th><th>Origin</th><th>Location</th><th>By</th><th></th></tr>` +
    rows.map((r) => `<tr><td>${r.id}</td><td>${badge(r.status)}</td><td>RPT-${r.report_id}</td><td>${esc(r.experiment)}</td><td>${esc(r.outcome)}</td><td>${esc(r.timepoint)}</td><td>${esc(r.arm)} <span class="muted small">${esc(r.arm_role)}</span></td><td>${esc(r.n)}</td><td>${esc(r.mean)}</td><td>${esc(r.sd)}</td><td>${esc(r.se)}</td><td>${esc(r.unit)}</td><td>${esc(r.value_origin)}${r.derivation ? `<div class="small muted">${esc(r.derivation)}</div>` : ""}</td><td class="small">${esc(r.source_location)}<div class="muted">${esc(r.source_passage || "")}</div></td><td class="small">${esc(r.actor)}${r.verified_by ? `<br>✓ ${esc(r.verified_by)}` : ""}</td>
    <td>${r.status !== "verified" ? `<button class="vf" data-id="${r.id}">Verify</button> <button class="ghost rj" data-id="${r.id}">Reject</button>` : ""}</td></tr>`).join("") + `</table>`;
  $$("#exList .vf").forEach((b) => b.onclick = () => guard(async () => { const v = $("#verifier").value.trim(); if (!v) throw new Error("Enter verifier name"); await op("verify_extraction", { row_id: Number(b.dataset.id), reviewer: v }); loadExtraction(); }));
  $$("#exList .rj").forEach((b) => b.onclick = () => guard(async () => { const v = $("#verifier").value.trim(); const n = prompt("Reason"); if (v && n) { await op("reject_extraction", { row_id: Number(b.dataset.id), reviewer: v, note: n }); loadExtraction(); } }));
  if (!$("#planJson").value) {
    const r = await op("analysis_checks", { review_id: REVIEW }).catch(() => null);
    $("#planJson").value = JSON.stringify(r ? r.analyses.map((a) => a.analysis).reduce((o, a) => (o.analyses.push(a), o), { analyses: [] }) : { analyses: [{ id: "A1", outcome: "LDL-C", outcome_aliases: [], timepoint: null, effect_measure: "SMD", method: "REML", inference: "knha", min_studies: 2, shared_control: "split" }] }, null, 2);
  }
  $("#analysisZip").href = `/api/reviews/${REVIEW}/analysis.zip?${tokenQS()}`;
}
$("#exForm").onsubmit = (e) => { e.preventDefault(); guard(async () => {
  const f = Object.fromEntries(new FormData(e.target)); const rid = f.report_id; delete f.report_id;
  for (const k of ["n", "mean", "sd", "se"]) f[k] = f[k] === "" ? null : Number(f[k]); for (const k in f) if (f[k] === "") delete f[k];
  const v = $("#verifier").value.trim(); if (!v) throw new Error("Enter your name in the verifier box");
  await op("enter_extraction", { review_id: REVIEW, report_id: rid, rows: [f], reviewer: v }); e.target.reset(); loadExtraction();
}); };
$("#savePlan").onclick = () => guard(async () => { const r = await op("save_analysis_plan", { review_id: REVIEW, plan: JSON.parse($("#planJson").value) }); toast(`Plan v${r.version} saved`); });
function flagsHTML(flags) { return flags.length ? `<ul class="notes">${flags.map((f) => `<li class="${f.severity === "block" ? "sev-unsupported" : "sev-semantic"}"><b>${esc(f.type)}</b> (${esc(f.severity)}): ${esc(f.message)}</li>`).join("")}</ul>` : `<div class="small b-ok">No pooling problems flagged.</div>`; }
$("#checkPlan").onclick = () => guard(async () => {
  const r = await op("analysis_checks", { review_id: REVIEW });
  $("#analysisOut").innerHTML = r.analyses.map((a) => `<div class="card"><h4>${esc(a.analysis.id)} · ${esc(a.analysis.outcome)} (${esc(a.analysis.effect_measure)})</h4><div>${a.data.length} comparisons ready</div>${flagsHTML(a.flags)}${a.excluded.length ? `<div class="small"><b>Not poolable:</b> ${esc(JSON.stringify(a.excluded))}</div>` : ""}</div>`).join("") + `<div class="small muted">${r.unverified_rows_ignored} unverified rows ignored.</div>`;
});
$("#runAnalysis").onclick = () => guard(async () => {
  const r = await op("export_analysis", { review_id: REVIEW });
  $("#analysisOut").innerHTML = r.analyses.map((a) => `<div class="card"><h4>${esc(a.id)} ${badge(a.status)}</h4><div>${a.comparisons} comparisons</div>${flagsHTML(a.flags)}
    ${a.summary ? `<div>Pooled estimate <b>${Number(a.summary.estimate).toFixed(3)}</b> [${Number(a.summary["ci.lb"]).toFixed(3)}, ${Number(a.summary["ci.ub"]).toFixed(3)}], p=${Number(a.summary.pval).toPrecision(3)}, τ²=${Number(a.summary.tau2).toPrecision(3)}, I²=${Number(a.summary.I2).toFixed(1)}%, k=${a.summary.k} (REML, Hartung–Knapp)</div><img class="forest" src="/api/analysis/${REVIEW}/${a.id}_forest.png?${tokenQS()}&t=${Date.now()}">` : ""}
    ${a.r && a.r.returncode ? `<pre class="small">${esc(a.r.stderr)}</pre>` : ""}</div>`).join("");
});

// ------------------------------------------------------------------ PRISMA & export
async function loadPrisma() {
  const svg = await fetch(`/api/reviews/${REVIEW}/prisma.svg?${tokenQS()}`).then((r) => r.text());
  $("#prismaSvg").innerHTML = svg;
  const c = await op("prisma_counts", { review_id: REVIEW });
  $("#prismaChecks").innerHTML = `<ul class="notes">${c.checks.map((k) => `<li class="${k.ok ? "b-ok" : "sev-unsupported"}">${k.ok ? "✓" : "✗"} ${esc(k.check)} (${k.left} vs ${k.right}) ${k.detail ? esc(k.detail.join("; ")) : ""}</li>`).join("")}</ul>`;
  $("#prismaCounts").textContent = JSON.stringify(c, null, 1);
  $("#prismaS").textContent = await op("prisma_s", { review_id: REVIEW });
  const upd = () => $("#packetZip").href = `/api/reviews/${REVIEW}/packet.zip?scope=${$("#packetScope").value}&full_text=${$("#packetFT").checked}&${tokenQS()}`;
  $("#packetScope").onchange = upd; $("#packetFT").onchange = upd; upd();
}

// ------------------------------------------------------------------ settings
async function loadSources() {
  const ss = await op("list_sources");
  const st = { public: ["b-ok", "connected · public"], configured: ["b-info", "key saved"], disconnected: ["b-bad", "not connected"], settings: ["b-violet", "settings"] };
  ss.sort((a, b) => (b.name === "contact") - (a.name === "contact"));  // contact e-mail first: Unpaywall needs it
  $("#sourcesList").innerHTML = ss.map((s) => { const [c, l] = st[s.status] || ["b-mute", s.status];
    return `<div class="card"><h4><span>${esc(s.platform)}</span><span class="badge ${c}">${l}</span></h4>
    <div class="small muted">${esc(s.role === "search" ? (s.register ? "trial register" : "search database") : s.role === "enrichment" ? "DOI enrichment / open access" : "")}${s.docs_url ? ` · <a href="${esc(s.docs_url)}" target="_blank" rel="noopener">docs</a>` : ""}${s.role === "search" && s.verified_live === false ? ` · <span style="color:var(--warn)">not yet verified live</span>` : ""}</div>
    ${s.access ? `<p class="small" style="color:var(--ink-2)">${esc(s.access)}</p>` : ""}
    ${Object.entries(s.credentials || {}).map(([k, v]) => `<label>${esc(v.label)}<div class="row tight"><input style="flex:1" type="${v.secret ? "password" : "text"}" data-src="${s.name}" data-key="${k}" placeholder="${v.configured ? (v.secret ? "•••••••• saved" : esc(v.value)) : "not set"}" autocomplete="off"><button class="primary compact savecred" data-src="${s.name}" data-key="${k}">Save</button>${v.configured ? `<button class="ghost compact danger clr" data-src="${s.name}" data-key="${k}">Clear</button>` : ""}</div></label>`).join("") || `<div class="small muted">No credentials needed.</div>`}
    ${s.name !== "contact" ? `<div class="row"><button class="ghost compact tc" data-src="${s.name}">Test connection</button><span class="small">${s.last_test ? `${s.last_test.ok ? `<span style="color:var(--ok)">✓</span>` : `<span style="color:var(--bad)">✗</span>`} ${esc(s.last_test.detail)} <span class="muted">${esc(s.last_test.tested_at.slice(0, 16).replace("T", " "))}</span>` : ""}</span></div>` : ""}</div>`; }).join("");
  $$("#sourcesList .savecred").forEach((b) => b.onclick = () => guard(async () => { const inp = $(`input[data-src="${b.dataset.src}"][data-key="${b.dataset.key}"]`); if (!inp.value) return; await op("set_credential", { source: b.dataset.src, key: b.dataset.key, value: inp.value }); inp.value = ""; toast("Saved to Keychain"); loadSources(); }));
  $$("#sourcesList .clr").forEach((b) => b.onclick = () => guard(async () => { await op("set_credential", { source: b.dataset.src, key: b.dataset.key, value: null }); loadSources(); }));
  $$("#sourcesList .tc").forEach((b) => b.onclick = () => guard(async () => { b.disabled = true; b.textContent = "Testing…"; await op("test_connection", { source: b.dataset.src }); loadSources(); }));
}

// ------------------------------------------------------------------ boot
(async () => {
  await guard(() => loadReviews());
  showTab(localStorage.getItem("ed_tab") || "search");
})();
