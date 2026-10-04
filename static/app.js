/* Job Scout front end. Vanilla JS - no build step. */

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  jobs: null, selected: null, config: null, exports: [],
  view: { q: "", min_score: 0, status: "all", source: "all", order: "score",
          starred: false, remote: false, sponsored: false, unviewed: false, startups: "any",
          employment: [], families: [], unclassified: false, salaryDisclosed: false,
          // keys of the user's own facet rules that are ticked
          rules: [] },
  // False until the saved sidebar has been restored - saving before that would
  // write the defaults above over the filters the person actually left set.
  viewReady: false,
  vocabulary: { employment: [], families: [] },
};

function viewPayload() {
  const v = state.view;
  return {
    q: v.q, min_score: v.min_score, status: v.status, source: v.source, order: v.order,
    starred: v.starred, remote: v.remote, sponsored: v.sponsored, unviewed: v.unviewed,
    startups: v.startups,
    salary_disclosed: v.salaryDisclosed, unclassified: v.unclassified,
    employment: v.employment, families: v.families, rules: v.rules || [],
  };
}

let lastSavedView = "";
let persistTimer = null;
function persistView() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const body = viewPayload();
    const signature = JSON.stringify(body);
    if (signature === lastSavedView) return;
    api("/api/view", { method: "POST", body })
      .then(() => { lastSavedView = signature; })
      .catch(() => {});   // a nicety, not core - never interrupt browsing over it
  }, 400);
}

// Push a saved view into state.view and every control that shows it.
function applyView(saved) {
  const s = saved || {};
  const v = state.view;
  v.q = s.q || "";
  v.min_score = +s.min_score || 0;
  v.status = s.status || "all";
  v.source = s.source || "all";
  v.order = s.order || "score";
  v.startups = ["hide", "only"].includes(s.startups) ? s.startups : "any";
  v.starred = !!s.starred; v.remote = !!s.remote; v.sponsored = !!s.sponsored;
  v.unviewed = !!s.unviewed; v.unclassified = !!s.unclassified;
  v.salaryDisclosed = !!s.salary_disclosed;
  v.employment = [...(s.employment || [])];
  v.families = [...(s.families || [])];
  v.rules = [...(s.rules || [])];

  $("#f-q").value = v.q;
  $("#f-score").value = v.min_score;
  $("#f-score-v").textContent = String(v.min_score);
  $("#f-order").value = v.order;
  $("#f-startups").value = v.startups;
  syncSelect("#f-status", "status");
  syncSelect("#f-source", "source");
  $("#f-star").checked = v.starred;
  $("#f-remote").checked = v.remote;
  $("#f-sponsored").checked = v.sponsored;
  $("#f-salary").checked = v.salaryDisclosed;
  $("#f-unclassified").checked = v.unclassified;
  $("#f-unviewed").checked = v.unviewed;
  // Force the facet checkboxes to redraw with the restored selection on next poll.
  Object.keys(facetState).forEach((k) => delete facetState[k]);
  if (typeof renderOwnFacets === "function" && typeof prof !== "undefined" && prof.rules) {
    renderOwnFacets();
  }
  lastSavedView = JSON.stringify(viewPayload());
}

// Status/source options arrive with the first stats poll, which can land after the
// saved view is restored; a saved value with no matching option yet would otherwise
// be silently dropped by the browser. And a saved source that has since vanished
// from the data falls back to "all" rather than filtering everything out.
function syncSelect(selector, key) {
  const sel = $(selector);
  if (!sel || sel.options.length <= 1) return;
  const wanted = state.view[key];
  const exists = Array.from(sel.options).some((o) => o.value === wanted);
  if (!exists) state.view[key] = "all";
  sel.value = state.view[key];
}

// Startup and account switches: fetch this person's saved sidebar and show it.
async function restoreSavedView() {
  state.viewReady = false;
  // A save still waiting from the previous account must not land in this one.
  clearTimeout(persistTimer);
  try {
    await loadConfig();
    applyView(state.config && state.config.view);
  } catch (err) {
    // No saved view is fine - the defaults above stand.
  }
  state.viewReady = true;
  await loadJobs();
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function toast(message, ms = 3600) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// A shape-of-a-report placeholder for the slower async panels (interview prep, CV
// review, a drafted cover letter) - these genuinely take several seconds, since they
// read a live advert or a real CV, so what shows while they do matters. Reused from
// deck.js and profile.js the same way esc()/api()/toast() already are.
function skeletonReport(caption) {
  return `<div class="block" aria-busy="true">
    ${caption ? `<div class="where" style="margin-bottom:10px">${esc(caption)}</div>` : ""}
    <div class="skel-para">
      <span class="skel skel-line w40" style="height:9px"></span>
      <span class="skel skel-line w90"></span>
      <span class="skel skel-line w70"></span>
    </div>
    <div class="skel-para">
      <span class="skel skel-line w40" style="height:9px"></span>
      <span class="skel skel-chip"></span><span class="skel skel-chip"></span>
      <span class="skel skel-chip" style="width:88px"></span>
    </div>
    <div class="skel-para">
      <span class="skel skel-line w40" style="height:9px"></span>
      <span class="skel skel-line w90"></span>
      <span class="skel skel-line w55"></span>
    </div>
  </div>`;
}

const fmtDate = (iso) => {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return "-";
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
};

/* ------------------------------------------------------------------- facets */
function labelFor(group, key) {
  const found = (state.vocabulary[group] || []).find((v) => v.key === key);
  return found ? found.label : key.replace(/_/g, " ");
}

// Signature of what a facet is showing, so the 4-second stats poll only redraws
// when something actually changed. Redrawing unconditionally destroyed the
// checkboxes under the user's cursor mid-click and reset the scroll position.
const facetState = {};

function renderFacet(hostId, group, counts, selected) {
  const host = $(hostId);
  const signature = JSON.stringify([counts, selected]);
  if (facetState[hostId] === signature) return;
  facetState[hostId] = signature;
  const options = state.vocabulary[group] || [];
  // Only offer categories the current results actually contain, commonest first,
  // plus anything already ticked so a selection never silently disappears.
  const rows = options
    .map((o) => ({ ...o, n: counts[o.key] || 0 }))
    .filter((o) => o.n > 0 || selected.includes(o.key))
    .sort((a, b) => b.n - a.n);

  if (!rows.length) {
    host.innerHTML = '<div class="none">Nothing classified yet</div>';
    return;
  }
  host.innerHTML = rows.map((o) => `<label>
      <input type="checkbox" value="${esc(o.key)}" ${selected.includes(o.key) ? "checked" : ""}>
      <span>${esc(o.label)}</span><span class="n">${o.n}</span>
    </label>`).join("");

  host.querySelectorAll("input").forEach((box) => {
    box.addEventListener("change", () => {
      const key = group === "employment" ? "employment" : "families";
      state.view[key] = Array.from(host.querySelectorAll("input:checked"))
        .map((b) => b.value);
      loadJobs();
    });
  });
}

/* ------------------------------------------------------------------ listing */
function scoreClass(n) { return n >= 60 ? "s-hi" : n >= 40 ? "s-mid" : "s-low"; }

// A row-shaped placeholder for the moment between page load and the first response -
// null means "nothing asked for yet", distinct from [] which means "asked, and there
// are none", so the empty-state message never flashes on first paint.
const SKELETON_ROWS = Array.from({ length: 7 }, (_, i) => `
  <tr class="skel-row" style="border-bottom:1px solid var(--line-soft)">
    <td style="padding:9px 12px"><span class="skel" style="width:14px;height:14px;display:inline-block"></span></td>
    <td style="padding:9px 12px">
      <span class="skel skel-line w${[70, 55, 90, 40, 70, 55, 90][i]}"></span>
      <span class="skel skel-line w40" style="height:9px"></span>
    </td>
    <td style="padding:9px 12px"><span class="skel skel-line w70"></span></td>
    <td style="padding:9px 12px"><span class="skel skel-line w55"></span></td>
    <td style="padding:9px 12px"><span class="skel skel-line w40"></span></td>
    <td style="padding:9px 12px"><span class="skel skel-chip"></span></td>
    <td style="padding:9px 12px"><span class="skel skel-line w55"></span></td>
  </tr>`).join("");

function renderRows() {
  const body = $("#rows");
  if (state.jobs === null) {
    body.innerHTML = SKELETON_ROWS;
    $("#empty").style.display = "none";
    return;
  }
  $("#empty").style.display = state.jobs.length ? "none" : "block";
  body.innerHTML = state.jobs.map((j) => {
    const signals = [];
    if (j.source_kind === "ats_direct") signals.push('<span class="pill direct">Direct</span>');
    // Shows what the posting's OWN TEXT says, not the raw "remote" flag the source
    // supplied - a widely reported complaint about Indeed and LinkedIn is a job listed
    // "Remote" that turns out, in the small print, to want two or three office days a
    // week. When the two disagree, the title carries a tooltip saying so.
    if (j.work_mode === "remote") {
      const mismatch = j.remote === false;
      signals.push(`<span class="pill remote"${mismatch
        ? ' title="Their description reads as remote even though it was not flagged that way"' : ""
        }>Remote</span>`);
    } else if (j.work_mode === "hybrid") {
      const mismatch = j.remote === true;
      signals.push(`<span class="pill hybrid"${mismatch
        ? ' title="Listed as Remote, but the posting itself asks for office days - shown as it actually reads"'
        : ""}>Hybrid</span>`);
    } else if (j.work_mode === "onsite" && j.remote) {
      signals.push('<span class="pill hybrid" title="Listed as Remote, but the posting '
        + 'itself reads as on-site - shown as it actually reads">On-site</span>');
    }
    if (j.long_open) {
      signals.push('<span class="pill caution" title="Open 45+ days. Worth checking it '
        + 'is still live before spending an evening on it - long-open postings are one '
        + 'of the clearest signs of a stale or already-filled listing">Long-open</span>');
    }
    // "New" is about triage status, but next to a posting you have already opened it
    // reads as "unseen", which it is not - the Status column still says "new" either way.
    if (j.status === "new" && !j.viewed) signals.push('<span class="pill new">New</span>');
    if (j.startup === true) signals.push('<span class="pill startup">Startup</span>');
    if (j.delisted) {
      signals.push('<span class="pill caution" title="The employer has taken this down, '
        + 'or its closing date has passed. Kept because you starred it or moved it on.">'
        + 'No longer listed</span>');
    }
    if (j.stability && j.stability !== "unknown") {
      signals.push(`<span class="pill stab-${esc(j.stability)}" title="Company stability, `
        + `from public records - open the posting for the evidence">${esc(j.stability_label)}</span>`);
    }
    if (j.employment_kind) {
      signals.push(`<span class="pill kind">${esc(labelFor("employment", j.employment_kind))}</span>`);
    }
    if (j.job_family) {
      signals.push(`<span class="pill family">${esc(labelFor("families", j.job_family))}</span>`);
    }
    if (j.sponsor_name) {
      signals.push(`<span class="pill sponsor" title="Licensed sponsor: ${esc(j.sponsor_name)}">` +
                   `Sponsor ${esc(j.sponsor_rating || "")}</span>`);
    }
    const rowClass = [state.selected === j.id ? "sel" : "", j.viewed ? "seen" : ""]
      .filter(Boolean).join(" ");
    const seenNote = j.viewed
      ? ` &middot; <span class="seen-tag" title="You first opened this ${
          esc(j.viewed_at ? new Date(j.viewed_at).toLocaleString("en-GB") : "")}">Opened ${
          fmtDate(j.viewed_at)}</span>`
      : "";
    return `<tr data-id="${j.id}" class="${rowClass}">
      <td><button class="star ${j.starred ? "on" : ""}" data-star="${j.id}"
           title="Star">${j.starred ? "★" : "☆"}</button></td>
      <td><div class="role">${esc(j.title)}</div>
          <div class="org">${esc(j.company)} &middot; ${esc(j.source)} &middot; ${fmtDate(j.posted_at)}${seenNote}</div></td>
      <td class="where">${esc(j.location || "-")}</td>
      <td class="where">${esc(j.salary_display || "-")}</td>
      <td><div class="scorecell ${scoreClass(j.score)}">
            <span class="scoreval">${j.score.toFixed(0)}</span>
            <span class="bar"><i style="width:${Math.min(100, j.score)}%"></i></span>
          </div></td>
      <td>${signals.join(" ") || '<span class="where">-</span>'}</td>
      <td><span class="pill">${esc(j.status)}</span></td>
    </tr>`;
  }).join("");

  body.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-star]")) return;
      openDrawer(tr.dataset.id);
    });
  });
  body.querySelectorAll("[data-star]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const { starred } = await api(`/api/job/${btn.dataset.star}/star`, { method: "POST" });
      btn.classList.toggle("on", starred);
      btn.textContent = starred ? "★" : "☆";
      const job = state.jobs.find((j) => j.id === btn.dataset.star);
      if (job) job.starred = starred;
    });
  });
}

const PAGE_SIZE = 300;
let jobsRequest = 0;

// append=true is "Show more": the next page of the same filters, added to the list.
async function loadJobs(append = false) {
  const v = state.view;
  const qs = new URLSearchParams({
    q: v.q, min_score: v.min_score, status: v.status, source: v.source,
    order: v.order, starred: v.starred ? "1" : "0", remote: v.remote ? "1" : "0",
    sponsored: v.sponsored ? "1" : "0", unclassified: v.unclassified ? "1" : "0",
    salary_disclosed: v.salaryDisclosed ? "1" : "0",
    unviewed: v.unviewed ? "1" : "0",
    startups: v.startups,
    limit: PAGE_SIZE,
    offset: append && state.jobs ? state.jobs.length : 0,
  });
  v.employment.forEach((k) => qs.append("employment", k));
  v.families.forEach((k) => qs.append("family", k));
  (v.rules || []).forEach((k) => qs.append("rule", k));
  if (state.viewReady && !append) persistView();
  // Ticking several boxes quickly fires several requests; one for an older set of
  // filters can come back last. Only the newest request may draw the list.
  const mine = ++jobsRequest;
  const data = await api(`/api/jobs?${qs}`);
  if (mine !== jobsRequest) return;
  state.jobs = append && state.jobs ? state.jobs.concat(data.jobs) : data.jobs;
  state.total = data.total ?? state.jobs.length;
  const shown = state.jobs.length;
  $("#result-count").textContent = shown < state.total
    ? `Showing ${shown.toLocaleString()} of ${state.total.toLocaleString()} positions`
    : `${state.total.toLocaleString()} position${state.total === 1 ? "" : "s"}`;
  const more = $("#btn-more");
  if (more) {
    more.style.display = shown < state.total ? "" : "none";
    more.textContent = `Show ${Math.min(PAGE_SIZE, state.total - shown).toLocaleString()} more`;
  }
  if (data.facets) {
    state.facets = data.facets;
    renderFacetCounts();
  }
  renderRows();
}

// Sidebar numbers for the filters currently set - from the last listing, not the
// whole database, so they always add up to what ticking that box would show.
function renderFacetCounts() {
  const f = state.facets;
  if (!f) return;
  renderFacet("#f-employment", "employment", f.employment || {}, state.view.employment);
  renderFacet("#f-family", "families", f.families || {}, state.view.families);
  $("#source-tally").innerHTML = Object.entries(f.sources || {})
    .map(([k, n]) => `${esc(k)} <b style="float:right">${n}</b>`).join("<br>");
  if (typeof renderOwnFacets === "function" && typeof prof !== "undefined" && prof.rules) {
    renderOwnFacets();
  }
}

async function loadStats() {
  const s = await api("/api/stats");
  $("#s-total").textContent  = s.total;
  $("#s-strong").textContent = s.strong_matches;
  $("#s-avg").textContent    = s.avg_score;

  const statusSel = $("#f-status");
  if (statusSel.options.length <= 1) {
    s.statuses.forEach((st) => statusSel.add(new Option(st, st)));
    syncSelect("#f-status", "status");
  }
  const sourceSel = $("#f-source");
  if (sourceSel.options.length <= 1) {
    Object.keys(s.by_source).forEach((src) => sourceSel.add(new Option(src, src)));
    const before = state.view.source;
    syncSelect("#f-source", "source");
    // The saved source is gone from the data - re-query rather than keep showing
    // results filtered to something that no longer exists.
    if (state.viewReady && before !== state.view.source) loadJobs();
  }
  if (s.vocabulary) state.vocabulary = s.vocabulary;
  // The sidebar numbers follow the current filters (see renderFacetCounts); the
  // whole-database stats only fill them in before the first listing arrives.
  if (!state.facets) {
    $("#source-tally").innerHTML = Object.entries(s.by_source)
      .map(([k, n]) => `${esc(k)} <b style="float:right">${n}</b>`).join("<br>");
    renderFacet("#f-employment", "employment", s.by_employment || {}, state.view.employment);
    renderFacet("#f-family", "families", s.by_family || {}, state.view.families);
  } else {
    renderFacetCounts();
  }
  // profile.js owns the custom facets; it may not have loaded on the first poll.
  if (s.own_rules && typeof prof !== "undefined") {
    prof.rules = s.own_rules;
    prof.counts = s.own_rule_counts || {};
    if (typeof renderOwnFacets === "function") renderOwnFacets();
  }

  const sp = s.sponsorship || {};
  const note = $("#f-sponsored-note");
  if (note) {
    const reg = sp.register || {};
    note.textContent = !sp.supported
      ? `No sponsor register published for ${sp.market || "this market"}`
      : !reg.available
        ? "Register not downloaded yet - set sponsorship in Criteria"
        : reg.organisations
          ? `${reg.organisations.toLocaleString()} licensed sponsors on file`
          : "Official register on file";
  }
  const sc = s.scrape;
  $("#scrape-bar").style.width =
    sc.running && sc.total ? `${(sc.done / sc.total) * 100}%` : "0";
  $("#btn-scrape").disabled = sc.running;
  $("#btn-scrape").textContent = sc.running ? (sc.message || "Scraping...") : "Run scrape";
  return s;
}


/* -------------------------------------------------------------- interview prep */
const prepCache = {};

function stageRow(stage) {
  return `<div class="prow">
    <div class="pk">${esc(stage.stage)}</div>
    <div class="pv"><b>${esc(stage.what)}</b>
      <div class="where">${esc(stage.evidence)}</div>
      <div class="advice">${esc(stage.prepare)}</div></div>
  </div>`;
}

function providerBlock(p) {
  const links = (p.practice || [])
    .map(([name, url, cost]) => url
      ? `<li><a href="${esc(url)}" target="_blank" rel="noopener">${esc(name)}</a>
           <span class="where">${esc(cost)}</span></li>`
      : `<li>${esc(name)} <span class="where">${esc(cost)}</span></li>`)
    .join("");
  return `<div class="provider">
    <b>${esc(p.name)}</b> <span class="where">${esc(p.why)}</span>
    <p>${esc(p.what)}</p>
    <ul class="links">${links}</ul>
  </div>`;
}

function questionBlock(q) {
  return `<div class="qrow">
    <div class="q"><span class="qkind ${esc(q.kind)}">${esc(q.kind.replace(/_/g, " "))}</span>
      ${esc(q.question)}</div>
    <div class="needs">${esc(q.needs)}</div>
  </div>`;
}

function prepHTML(b) {
  const packs = (b.employer_packs || []).length
    ? `<div class="block hit">
         <div class="micro">Practice papers published for ${esc(b.company)}</div>
         <ul class="links">${b.employer_packs.map((p) => `<li>
           <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.test)}</a>
           <span class="where">same format they send</span></li>`).join("")}</ul>
       </div>` : "";

  const values = (b.values || []).length
    ? `<div class="block">
         <div class="micro">Their own stated values &mdash; these become questions</div>
         <div class="chips">${b.values.map((v) => `<span class="chip">${esc(v)}</span>`).join("")}</div>
         <div class="where" style="margin-top:6px">${esc(b.values_note)}
           ${b.values_source ? `<a href="${esc(b.values_source)}" target="_blank"
             rel="noopener">source</a>` : ""}</div>
       </div>`
    : `<div class="block"><div class="where">${esc(b.values_note)}</div></div>`;

  const evidence = (b.your_evidence || []).length
    ? `<div class="block">
         <div class="micro">Your own material, and which question it answers</div>
         ${b.your_evidence.map((e) => `<div class="prow ${e.gap ? "gap" : ""}">
           <div class="pk">${esc(e.skill)}</div>
           <div class="pv"><b>${esc(e.use_for)}</b>
             <div class="advice">${esc(e.prepare)}</div></div></div>`).join("")}
       </div>` : "";

  return `
    ${packs}
    <div class="block">
      <div class="micro">Stages this advert actually names</div>
      ${(b.stages || []).length ? b.stages.map(stageRow).join("")
        : '<div class="where">None. That usually means a normal interview.</div>'}
      <div class="micro" style="margin-top:12px">Likely shape of the process (a guess)</div>
      <ol class="stages">${(b.likely_stages || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
    </div>
    ${(b.providers || []).length ? `<div class="block">
      <div class="micro">Test providers this employer tends to use</div>
      ${b.providers.map(providerBlock).join("")}</div>` : ""}
    ${values}
    ${evidence}
    <div class="block">
      <div class="micro">Questions to be ready for &mdash; ${b.questions.length} of them
        <span class="where">(by ${esc(b.engine)})</span></div>
      ${b.questions.map(questionBlock).join("")}
    </div>
    <div class="block">
      <div class="micro">Revise, in this order</div>
      <ol class="topics">${(b.topics || []).map((t) => `<li><b>${esc(t.topic)}</b>
        <div class="where">${esc(t.why)}</div></li>`).join("")}</ol>
    </div>
    <div class="block">
      <div class="micro">Openers worth having ready</div>
      <ul class="plain">${(b.opening_lines || []).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    </div>
    <div class="block">
      <div class="micro">Where other candidates prepare</div>
      <ul class="links">${(b.resources || []).map((r) => `<li>
        <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>
        <span class="where">${esc(r.cost)}</span>
        <div class="where">${esc(r.what)}</div></li>`).join("")}</ul>
    </div>
    <div class="block caveats">
      ${(b.caveats || []).map((c) => `<div class="warn soft">${esc(c)}</div>`).join("")}
    </div>`;
}

async function loadPrep(jobId, hostSelector) {
  const host = $(hostSelector);
  if (prepCache[jobId]) { host.innerHTML = prepHTML(prepCache[jobId]); return; }
  host.innerHTML = skeletonReport(
    "Reading the advert, their values page and the practice papers published for them…");
  try {
    const brief = await api(`/api/job/${jobId}/interview`);
    prepCache[jobId] = brief;
    host.innerHTML = prepHTML(brief);
  } catch (err) {
    host.innerHTML = `<div class="warn">${esc(err.message)}</div>`;
  }
}

/* -------------------------------------------------------------------- drawer */
// Company stability: the level, then every fact behind it and where it came from, then
// what could not be checked - so the reader can judge it rather than take it on trust.
function stabilityBlock(s) {
  if (!s) return "";
  const sign = { "+": "plus", "-": "minus", "=": "even" };
  const factors = (s.factors || []).map((f) => `<li class="stab-f ${sign[f.effect] || "even"}">
      <b>${esc(f.label)}</b> &mdash; ${esc(f.detail)}
      <span class="where">${f.url && /^https:\/\//.test(f.url)
        ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.source)}</a>`
        : esc(f.source)}</span></li>`).join("");
  const gaps = (s.not_checked || []).map((n) => `<li>${esc(n)}</li>`).join("");
  return `<div class="section micro">Company stability</div>
    <div><span class="pill stab-${esc(s.level)}">${esc(s.label)}</span></div>
    ${factors ? `<ul class="stab-list">${factors}</ul>` : ""}
    ${gaps ? `<div class="where" style="margin-top:6px">Not checked:</div>
      <ul class="stab-gaps">${gaps}</ul>` : ""}
    <div class="where" style="margin-top:6px;font-size:11px">${esc(s.disclaimer)}</div>`;
}

async function openDrawer(id) {
  state.selected = id;
  // The server stamps it when the detail loads below; mirroring that here means the
  // row reads "Opened today" straight away instead of after the next list reload.
  const listed = (state.jobs || []).find((j) => j.id === id);
  if (listed && !listed.viewed) {
    listed.viewed = true;
    listed.viewed_at = new Date().toISOString();
  }
  renderRows();
  $("#drawer").classList.add("open");
  $("#pane-overview").innerHTML = skeletonReport();
  $("#pane-contacts").innerHTML = "";
  $("#pane-prep").innerHTML =
    '<button class="btn primary" id="btn-prep">Build the interview brief</button>'
    + '<div class="where" style="margin-top:8px">Reads this advert, the employer\'s own '
    + 'values page and the practice papers published for them. Takes a few seconds.</div>';
  $("#btn-prep").addEventListener("click", () => loadPrep(id, "#pane-prep"));

  const { job, outreach, company, stability, company_confirmed } = await api(`/api/job/${id}`);
  if (listed && stability) {           // keep the row pill in step with what was just computed
    listed.stability = stability.level;
    listed.stability_label = stability.label;
  }

  $("#d-company").textContent = job.company;
  $("#d-title").textContent   = job.title;
  $("#d-meta").textContent    =
    [job.location, job.employment_type, job.department].filter(Boolean).join("  ·  ");

  const statusOptions = ["new", "shortlisted", "applied", "interviewing", "offer",
                         "rejected", "dismissed"]
    .map((s) => `<option ${s === job.status ? "selected" : ""}>${s}</option>`).join("");

  $("#pane-overview").innerHTML = `
    <dl class="kv">
      <dt>Score</dt><dd><b style="font-size:15px">${job.score.toFixed(0)}</b> / 100</dd>
      <dt>Salary</dt><dd>${esc(job.salary_display || "not stated")}</dd>
      <dt>Posted</dt><dd>${fmtDate(job.posted_at)}</dd>
      <dt>Source</dt><dd>${esc(job.source)} ${job.source_kind === "ats_direct"
        ? '<span class="pill direct">company board</span>' : ""}</dd>
      <dt>Status</dt><dd><select id="d-status">${statusOptions}</select></dd>
      <dt>Tracker</dt><dd id="d-track">${trackCell(job)}</dd>
      ${job.sponsor_name ? `<dt>Visa sponsor</dt><dd>
        <b>${esc(job.sponsor_rating)} rating</b> &mdash; listed as
        &ldquo;${esc(job.sponsor_name)}&rdquo;
        <div class="where" style="margin-top:3px">${esc(job.sponsor_routes || "")}</div>
        ${job.sponsor_conf < 0.7 ? '<div class="caveat">Name match is approximate &mdash; check this is the same company.</div>' : ""}
      </dd>` : ""}
    </dl>
    ${job.delisted ? `<div class="caveat">No longer listed &mdash; the employer has taken
      this down or its closing date has passed (noticed ${fmtDate(job.delisted_at)}).</div>` : ""}
    <a class="btn primary" href="${esc(job.url)}" target="_blank" rel="noopener">Open posting</a>

    <div class="section micro">Why it scored this</div>
    <ul class="reasons">${(job.score_reasons || [])
      .map((r) => `<li>${esc(r)}</li>`).join("") || "<li>No specific signals</li>"}</ul>

    ${job.matched_skills?.length ? `<div class="section micro">Your CV covers</div>
      <div class="chips">${job.matched_skills.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>` : ""}
    ${job.missing_skills?.length ? `<div class="section micro">Mentioned, not on your CV</div>
      <div class="chips">${job.missing_skills.map((s) => `<span class="chip gap">${esc(s)}</span>`).join("")}</div>` : ""}

    ${company && (company.description || company.industry?.length || company.size_band) ? `
      <div class="section micro">About ${esc(job.company)}</div>
      <div class="where" style="line-height:1.55">
        ${company.description ? esc(company.description) + "<br>" : ""}
        ${company.size_band ? `<b>${esc(company.size_band)}</b>` : ""}
        ${company.founded ? ` &middot; founded ${esc(company.founded)}` : ""}
        ${company.headquarters ? ` &middot; HQ ${esc(company.headquarters)}` : ""}
      </div>
      ${company.industry?.length ? `<div class="chips" style="margin-top:6px">${
        company.industry.map((i) => `<span class="chip">${esc(i)}</span>`).join("")}</div>` : ""}
      ${company.website ? `<div style="margin-top:6px"><a href="${esc(company.website)}"
        target="_blank" rel="noopener">${esc(company.website)}</a></div>` : ""}
      ${company.source === "wikidata" && !company_confirmed ? `<div class="caveat">
        Matched by name only &mdash; this may be a different organisation with the same
        name, so check before relying on it.</div>` : ""}
      <div class="where" style="margin-top:5px;font-size:11px">
        Source: ${esc(company.source || "wikidata")}. Glassdoor closed its API to
        developers in 2022, so ratings and reviews are not available here.
      </div>` : ""}

    ${stabilityBlock(stability)}

    <div class="section micro">Description</div>
    <div class="desc">${esc(job.description || "No description supplied by the source.")}</div>`;

  $("#d-status").addEventListener("change", async (e) => {
    await api(`/api/job/${id}/status`, { method: "POST", body: { status: e.target.value } });
    toast(`Marked ${e.target.value}`);
    loadJobs(); loadStats(); refreshTrackerIfShown();
    wireTrackCell(id, (await api(`/api/job/${id}`)).job);
  });
  wireTrackCell(id, job);

  const drafts = outreach.drafts || {};
  $("#pane-contacts").innerHTML = `
    <div class="notice">${esc(outreach.disclosure)}</div>
    ${(outreach.published_contacts || []).length ? `
      <div class="section micro">Named on the posting itself</div>
      ${outreach.published_contacts.map((c) => `<div class="contact">
        <div class="label">${esc(c.value)}</div>
        <div class="why">${esc(c.note)}</div></div>`).join("")}` : ""}
    <div class="section micro">Search LinkedIn for these people</div>
    ${(outreach.links || []).map((l) => {
      const d = drafts[l.role];
      return `<div class="contact">
        <div class="top">
          <div><div class="label">${esc(l.label)}</div>
               <div class="why">${esc(l.why)}</div>
               ${l.caveat ? `<div class="caveat">${esc(l.caveat)}</div>` : ""}</div>
          <a class="btn sm" href="${esc(l.url)}" target="_blank" rel="noopener">Open</a>
        </div>
        ${d ? `<div class="draft">${esc(d.connection_note)}</div>
               <button class="btn sm" style="margin-top:7px"
                 data-copy="${esc(d.message)}">Copy longer message</button>` : ""}
      </div>`;
    }).join("")}`;

  $$("#pane-contacts [data-copy]").forEach((b) =>
    b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.copy);
      toast("Message copied to clipboard");
    }));

  $("#pane-notes").innerHTML = `
    <div class="field"><label class="micro">Your notes on this role</label>
      <textarea id="d-note" rows="10">${esc(job.notes || "")}</textarea></div>
    <button class="btn primary" id="d-note-save">Save note</button>`;
  $("#d-note-save").addEventListener("click", async () => {
    await api(`/api/job/${id}/note`, { method: "POST", body: { note: $("#d-note").value } });
    toast("Note saved");
  });
}

/* ------------------------------------------------------------------ criteria */
function tagList(el, items, onRemove) {
  el.innerHTML = items.map((t, i) =>
    `<span class="tag">${esc(t)}<button data-i="${i}">&times;</button></span>`).join("");
  el.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => onRemove(Number(b.dataset.i))));
}

function renderCountry() {
  const sel = $("#c-country");
  const list = state.config.countries || [];
  const current = state.config.search.country ||
                  (state.config.home ? state.config.home.code : "");
  sel.innerHTML = '<option value="">Auto-detect from locations</option>' +
    list.map((c) => `<option value="${c.code}" ${c.code === current ? "selected" : ""}>` +
                    `${esc(c.name)} (${c.currency})</option>`).join("");
  $("#c-stage").value = state.config.search.career_stage || "student";
  $("#c-visa").value = state.config.search.visa_sponsorship || "any";
  const modes = state.config.search.work_modes || [];
  $$(".c-mode").forEach((cb) => { cb.checked = modes.includes(cb.value); });
  const home = state.config.home;
  $("#c-country-note").textContent = home
    ? `Resolved to ${home.name}. Salaries read as ${home.currency}. ` +
      `Adzuna ${home.adzuna ? "covers" : "does not cover"} this market.`
    : "No market resolved - add a location or pick a country.";
}

function renderCriteria() {
  const s = state.config.search;
  renderCountry();
  const bind = (elId, key) => tagList($(elId), s[key], (i) => {
    s[key].splice(i, 1); renderCriteria();
  });
  bind("#c-titles", "titles");
  bind("#c-keywords", "keywords_any");
  bind("#c-excluded", "keywords_excluded");
  bind("#c-locations", "locations");

  $("#c-minscore").value = state.config.min_score;
  $("#c-maxage").value   = s.max_age_days ?? "";
  $("#c-salary").value   = s.salary_min ?? "";
  $("#c-remote").checked = !!s.remote_ok;
  $("#c-senior").checked = !!s.exclude_senior;

  $("#weights").innerHTML = Object.entries(state.config.scoring.weights)
    .map(([k, v]) => `<div class="field">
      <label class="micro">${esc(k.replace("_", " "))}</label>
      <input type="number" data-w="${esc(k)}" value="${v}" min="0" max="60" step="1">
    </div>`).join("");
}

async function loadConfig() {
  state.config = await api("/api/config");
  renderCriteria();
  const auto = ["Company", "Role Name", "Deadline", "Application Link", "Location",
                "Salary", "Duration", "Open Date", "Source"];
  const manual = ["Start Date", "CV", "Cover Letter", "Applied?", "Date Applied",
                  "Status", "Got as far as", "Next Action", "Next Action Date", "Notes"];
  $("#cols-auto").innerHTML = auto.map((c) => `<span class="tag">${c}</span>`).join("");
  $("#cols-manual").innerHTML = manual.map((c) => `<span class="tag">${c}</span>`).join("");
}

function addTagInput(inputId, key) {
  $(inputId).addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.value.trim()) return;
    state.config.search[key].push(e.target.value.trim());
    e.target.value = "";
    renderCriteria();
  });
}

/* ----------------------------------------------------------------- assistant */
function pushMessage(role, html) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="who">${role === "user" ? "YOU" : "JS"}</div>
                  <div class="body">${html}</div>`;
  $("#chatlog").appendChild(el);
  $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
}

async function sendChat(text) {
  if (!text.trim()) return;
  pushMessage("user", esc(text));
  $("#chat-input").value = "";
  try {
    const r = await api("/api/assistant", {
      method: "POST", body: { message: text, view: state.view, confirm: true },
    });
    const chips = (r.applied || []).map((a) => `<span class="pill">${esc(a)}</span>`).join(" ");
    const engine = r.engine === "claude" ? "Claude" : "rules engine";
    pushMessage("bot", `${esc(r.reply)}
      ${chips ? `<div class="meta">${chips}</div>` : ""}
      <div class="meta"><span class="micro">via ${engine}</span></div>`);

    Object.assign(state.view, {
      q: r.view.search ?? state.view.q,
      order: r.view.order ?? state.view.order,
      status: r.view.status ?? state.view.status,
      source: r.view.source ?? state.view.source,
    });
    $("#f-q").value = state.view.q;
    $("#f-order").value = state.view.order;
    if (r.export) registerExport(r.export);
    if (r.config_changed) await loadConfig();
    await loadJobs(); await loadStats();
  } catch (err) {
    pushMessage("bot", `<span style="color:var(--red)">${esc(err.message)}</span>`);
  }
}

/* ------------------------------------------------------------------ follow-ups */
async function loadFollowups() {
  let data;
  try {
    data = await api("/api/followups");
  } catch (err) {
    return; // quiet failure - this is a nudge, not core functionality
  }
  const panel = $("#panel-followups");
  if (!data.count) { panel.style.display = "none"; return; }
  panel.style.display = "";
  $("#followups-body").innerHTML = data.jobs.map((j) => `
    <div class="prow">
      <div class="pk">${esc(j.company)}</div>
      <div class="pv">
        <b>${esc(j.title)}</b>
        <div class="where">Applied ${Math.floor(j.quiet_days)} days ago &middot;
          ${esc(j.location || "location not stated")}</div>
        <div class="rowbtns" style="margin-top:6px">
          <a class="btn sm" href="${esc(j.url || "#")}" target="_blank" rel="noopener">
            Reopen the posting</a>
          <button class="btn sm" data-chase="${j.id}">Mark as chased</button>
          <button class="btn sm" data-close="${j.id}">No longer pursuing</button>
        </div>
      </div>
    </div>`).join("");

  // "Chased" just resets the clock - status stays "applied" so a genuine follow-up
  // message you actually sent buys another 21 days before this nags you again.
  $$("#followups-body [data-chase]").forEach((btn) => btn.addEventListener("click", async () => {
    await api(`/api/job/${btn.dataset.chase}/status`, { method: "POST", body: { status: "applied" } });
    toast("Noted - back in 21 days if it is still quiet.");
    loadFollowups();
  }));
  $$("#followups-body [data-close]").forEach((btn) => btn.addEventListener("click", async () => {
    await api(`/api/job/${btn.dataset.close}/status`, { method: "POST", body: { status: "rejected" } });
    toast("Moved off your active list.");
    loadFollowups();
    loadStats();
  }));
}

/* ---------------------------------------------------------- my applications */
// The user's own tracker: every job they have shortlisted, applied to or imported from
// their spreadsheet. Small (tens to hundreds of rows), so it is fetched whole and
// filtered here rather than paged through the server like the jobs list.
const TRK_GROUPS = {
  all: () => true,
  todo: (s) => s === "Not applied yet",
  applied: (s) => s !== "Not applied yet",
  progress: (s) => ["Applied", "Online test", "1st interview", "2nd / AC", "Final round"].includes(s),
  offers: (s) => s === "Offer" || s === "Accepted",
  closed: (s) => ["Rejected", "Ghosted", "Withdrawn"].includes(s),
};
const TRK_STAGE_CLASS = { "Not applied yet": "todo", Offer: "offer", Accepted: "offer",
  Rejected: "closed", Ghosted: "closed", Withdrawn: "closed" };
const trk = { jobs: [], statuses: [], noteTimers: {} };

// A drawer opened over the Tracker tab must not leave the list behind it stale.
function refreshTrackerIfShown() {
  if ($("#view-tracker")?.classList.contains("on")) loadTracker();
}

function trackCell(job) {
  if (!job.tracked) {
    return '<button class="btn sm" id="d-track-add">Add to tracker</button>';
  }
  const opts = (trk.statuses.length ? trk.statuses : [job.app_status])
    .map((s) => `<option ${s === job.app_status ? "selected" : ""}>${esc(s)}</option>`).join("");
  return `<select id="d-track-stage" aria-label="Tracker status">${opts}</select>`;
}

function wireTrackCell(id, job) {
  const cell = $("#d-track");
  if (!cell) return;
  cell.innerHTML = trackCell(job);
  const sync = async (body, message) => {
    const res = await api(`/api/job/${id}/track`, { method: "POST", body });
    toast(message(res));
    const status = $("#d-status");
    if (status) status.value = res.status;
    loadJobs(); loadStats(); refreshTrackerIfShown();
    wireTrackCell(id, { ...job, ...res, tracked: true });
  };
  $("#d-track-add")?.addEventListener("click", () =>
    sync({}, () => "Added to My applications on the Tracker tab"));
  $("#d-track-stage")?.addEventListener("change", (e) =>
    sync({ app_status: e.target.value }, (r) => `Tracker: ${r.app_status}`));
}

async function loadTracker() {
  try {
    const data = await api("/api/tracker");
    trk.jobs = data.jobs;
    trk.statuses = data.statuses;
  } catch (err) {
    $("#trk-empty").hidden = false;
    $("#trk-empty").textContent = `Could not load your applications: ${err.message}`;
    return;
  }
  $("#trk-add-status").innerHTML = trk.statuses.map((s) => `<option>${esc(s)}</option>`).join("");
  renderTracker();
}

function trkDeadline(iso) {
  if (!iso) return '<span class="where">&mdash;</span>';
  const day = new Date(iso.slice(0, 10) + "T00:00:00");
  if (isNaN(day)) return esc(iso);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const left = Math.round((day - today) / 86400000);
  const label = day.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  const hint = left < 0 ? "passed" : left === 0 ? "today" : `${left} day${left === 1 ? "" : "s"}`;
  const cls = left < 0 ? "trk-late" : left <= 7 ? "trk-soon" : "where";
  return `${label}<div class="${cls}">${hint}</div>`;
}

function renderTracker() {
  const group = $("#trk-filter").value;
  const q = $("#trk-q").value.trim().toLowerCase();
  // counts follow the search box, so each option says what picking it would show
  const searched = trk.jobs.filter((j) => !q
    || `${j.company} ${j.title} ${j.location || ""}`.toLowerCase().includes(q));
  $$("#trk-filter option").forEach((o) => {
    const n = searched.filter((j) => TRK_GROUPS[o.value](j.app_status)).length;
    o.textContent = `${o.textContent.replace(/ \(\d+\)$/, "")} (${n})`;
  });
  const shown = searched.filter((j) => TRK_GROUPS[group](j.app_status));
  const applied = trk.jobs.filter((j) => j.app_status !== "Not applied yet").length;
  $("#trk-count").textContent = trk.jobs.length
    ? `${trk.jobs.length} tracked · ${applied} applied · ${trk.jobs.length - applied} not yet` : "";

  const empty = $("#trk-empty");
  empty.hidden = shown.length > 0;
  empty.textContent = !trk.jobs.length
    ? "Nothing here yet. Import your spreadsheet, or add a job from the Positions tab."
    : "No applications match this filter.";
  $(".trk-wrap").hidden = !shown.length;

  const opts = (cur) => trk.statuses.map((s) =>
    `<option ${s === cur ? "selected" : ""}>${esc(s)}</option>`).join("");
  $("#trk-body").innerHTML = shown.map((j) => `
    <tr data-id="${esc(j.id)}" class="trk-${TRK_STAGE_CLASS[j.app_status] || "live"}">
      <td><b>${esc(j.company)}</b>
        ${j.location ? `<div class="where">${esc(j.location)}</div>` : ""}</td>
      <td>${j.url ? `<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>`
                  : esc(j.title)}
        ${trkFacts(j)}
        ${j.source_kind === "manual" ? '<div class="where">added by you</div>' : ""}
        ${j.delisted ? '<div class="trk-late">no longer listed</div>' : ""}</td>
      <td class="trk-nowrap">${trkDeadline(j.closes_at)}</td>
      <td><select data-stage aria-label="Status for ${esc(j.company)}">${opts(j.app_status)}</select></td>
      <td><input type="date" data-applied value="${esc((j.applied_at || "").slice(0, 10))}"
             aria-label="Date applied"></td>
      <td><textarea data-note rows="2" aria-label="Notes for ${esc(j.company)}"
             placeholder="Notes">${esc(j.notes || "")}</textarea>
          <div class="where trk-saved"></div></td>
      <td class="trk-nowrap">
        ${j.url ? '<button class="btn sm" data-fill title="Fill in the blanks from the posting">Auto-fill</button>' : ""}
        ${j.source_kind === "manual" ? "" : '<button class="btn sm" data-open>Details</button>'}
        <button class="btn sm danger" data-remove title="Take off the tracker">Remove</button></td>
    </tr>`).join("");
}

// Salary, duration and start date - the details auto-fill adds that have no column here.
function trkFacts(j) {
  const extra = j.tracker_extra || {};
  const start = extra["Start Date"];
  const startText = /^\d{4}-\d{2}-\d{2}/.test(start || "")
    ? new Date(start.slice(0, 10) + "T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" })
    : start;
  const facts = [j.salary_display, extra.Duration, startText && `starts ${startText}`].filter(Boolean);
  return facts.length ? `<div class="where">${facts.map(esc).join(" · ")}</div>` : "";
}

async function autofillOne(id) {
  const res = await api(`/api/job/${id}/autofill`, { method: "POST" });
  const job = trk.jobs.find((j) => j.id === id);
  if (job && res.job) Object.assign(job, res.job);
  return res;
}

// Fills every row in view, a few at a time, so one slow careers site does not hold
// up the rest.
async function autofillAll() {
  const button = $("#trk-autofill");
  const out = $("#trk-import-result");
  const group = $("#trk-filter").value;
  const q = $("#trk-q").value.trim().toLowerCase();
  const rows = trk.jobs.filter((j) => j.url && TRK_GROUPS[group](j.app_status) && (!q
    || `${j.company} ${j.title} ${j.location || ""}`.toLowerCase().includes(q)));
  if (!rows.length) {
    out.innerHTML = '<div class="caveat">No jobs in view have a link to read.</div>';
    return;
  }
  button.disabled = true;
  const results = [];
  let done = 0;
  const progress = () => {
    out.innerHTML = `<div class="notice">Reading postings&hellip; ${done} of ${rows.length}</div>`;
  };
  progress();
  const queue = [...rows];
  const worker = async () => {
    while (queue.length) {
      const job = queue.shift();
      try {
        results.push({ job, ...(await autofillOne(job.id)) });
      } catch (err) {
        results.push({ job, filled: {}, message: err.message, failed: true });
      }
      done += 1;
      progress();
    }
  };
  await Promise.all([worker(), worker(), worker()]);
  button.disabled = false;

  const filled = results.filter((r) => Object.keys(r.filled || {}).length);
  const fields = filled.reduce((n, r) => n + Object.keys(r.filled).length, 0);
  const unread = results.filter((r) => !Object.keys(r.filled || {}).length
    && !/^Already complete/.test(r.message || ""));
  out.innerHTML = `<div class="notice">
    Filled <b>${fields}</b> detail${fields === 1 ? "" : "s"} on <b>${filled.length}</b> of
    ${rows.length} job${rows.length === 1 ? "" : "s"}${
    results.length - filled.length - unread.length
      ? `; ${results.length - filled.length - unread.length} already complete` : ""}.
    Blanks only - nothing you typed was changed.
    <button class="btn sm primary" data-trk-download>Download as Excel</button>
    ${unread.length ? `<div class="where" style="margin-top:4px">Could not fill ${unread.length}:
      ${unread.map((r) => `${esc(r.job.company)} (${esc(r.message)})`).join("; ")}</div>` : ""}
  </div>`;
  if (document.activeElement && document.activeElement.matches("[data-note]")) {
    document.activeElement.blur();        // save a note being typed before redrawing
  }
  renderTracker();
}

function trkRow(el) {
  const tr = el.closest("tr[data-id]");
  return tr && { tr, id: tr.dataset.id, job: trk.jobs.find((j) => j.id === tr.dataset.id) };
}

async function saveTrackerNote(id, text, tr) {
  clearTimeout(trk.noteTimers[id]);
  delete trk.noteTimers[id];
  const job = trk.jobs.find((j) => j.id === id);
  if (!job || job.notes === text) return;
  try {
    await api(`/api/job/${id}/note`, { method: "POST", body: { note: text } });
    job.notes = text;
    const mark = tr && tr.querySelector(".trk-saved");
    if (mark) { mark.textContent = "Saved"; setTimeout(() => { mark.textContent = ""; }, 1500); }
  } catch (err) {
    toast(`Could not save the note: ${err.message}`);
  }
}

async function importTracker(file) {
  const out = $("#trk-import-result");
  out.innerHTML = `<div class="notice">Reading ${esc(file.name)}&hellip;</div>`;
  const form = new FormData();
  form.append("file", file);
  let res;
  try {
    const r = await fetch("/api/tracker/import",
      { method: "POST", body: form, headers: { "X-Job-Scout": "1" } });
    res = await r.json();
  } catch (err) {
    res = { error: err.message };
  }
  if (res.error) {
    out.innerHTML = `<div class="caveat">${esc(res.error)}</div>`;
    return;
  }
  const unknown = Object.keys(res.unknown_statuses || {});
  out.innerHTML = `<div class="notice">
    Imported <b>${esc(file.name)}</b> (sheet &ldquo;${esc(res.sheet)}&rdquo;):
    <b>${res.added}</b> added, <b>${res.updated}</b> already here and updated${
    res.skipped ? `, ${res.skipped} skipped for having no company (rows ${res.skipped_rows.join(", ")})` : ""}.
    <div class="where" style="margin-top:4px">Read columns: ${
      Object.keys(res.columns).map(esc).join(", ")}${
      res.kept_extra_columns.length ? `. Also kept: ${res.kept_extra_columns.map(esc).join(", ")}` : ""}.
    ${unknown.length ? `Statuses it did not recognise (${unknown.map(esc).join(", ")}) were
      set from the Applied column and copied into the notes.` : ""}</div></div>`;
  await loadTracker();
  loadJobs(); loadStats();
}

function wireTracker() {
  $("#trk-filter").addEventListener("change", renderTracker);
  $("#trk-q").addEventListener("input", debounce(renderTracker, 150));
  const body = $("#trk-body");

  body.addEventListener("change", async (e) => {
    const row = trkRow(e.target);
    if (!row) return;
    try {
      if (e.target.matches("[data-stage]")) {
        const res = await api(`/api/job/${row.id}/track`,
          { method: "POST", body: { app_status: e.target.value } });
        Object.assign(row.job, res);
        toast(`${row.job.company}: ${res.app_status}`);
        renderTracker();
        loadJobs(); loadStats();
      } else if (e.target.matches("[data-applied]")) {
        const res = await api(`/api/job/${row.id}/track`,
          { method: "POST", body: { applied_at: e.target.value } });
        Object.assign(row.job, res);
      }
    } catch (err) {
      toast(`Could not save: ${err.message}`);
    }
  });
  body.addEventListener("input", (e) => {
    if (!e.target.matches("[data-note]")) return;
    const row = trkRow(e.target);
    clearTimeout(trk.noteTimers[row.id]);
    trk.noteTimers[row.id] = setTimeout(
      () => saveTrackerNote(row.id, e.target.value, row.tr), 700);
  });
  body.addEventListener("focusout", (e) => {
    if (!e.target.matches("[data-note]")) return;
    const row = trkRow(e.target);
    if (row) saveTrackerNote(row.id, e.target.value, row.tr);
  });
  body.addEventListener("click", async (e) => {
    const row = trkRow(e.target);
    if (!row) return;
    if (e.target.matches("[data-open]")) {
      openDrawer(row.id);
    } else if (e.target.matches("[data-fill]")) {
      e.target.disabled = true;
      e.target.textContent = "Reading…";
      try {
        const res = await autofillOne(row.id);
        toast(`${row.job.company}: ${res.message}`);
      } catch (err) {
        toast(`Could not auto-fill: ${err.message}`);
      }
      renderTracker();
    } else if (e.target.matches("[data-remove]")) {
      const manual = row.job.source_kind === "manual";
      if (!confirm(manual
        ? `Delete ${row.job.company} - ${row.job.title} and its notes? You added it, so it is removed for good.`
        : `Take ${row.job.company} - ${row.job.title} off your tracker? It goes back to the jobs list and your notes are kept.`)) return;
      await api(`/api/job/${row.id}/untrack`, { method: "POST" });
      trk.jobs = trk.jobs.filter((j) => j.id !== row.id);
      renderTracker();
      loadJobs(); loadStats();
    }
  });

  $("#trk-autofill").addEventListener("click", autofillAll);
  $("#trk-import-result").addEventListener("click", (e) => {
    if (e.target.matches("[data-trk-download]")) doExport("tracker");
  });
  $("#trk-import-btn").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#trk-file").click(); }
  });
  $("#trk-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) importTracker(file);
  });

  $("#trk-add-toggle").addEventListener("click", () => {
    const form = $("#trk-add");
    form.hidden = !form.hidden;
    $("#trk-add-toggle").setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.company.focus();
  });
  $("#trk-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = Object.fromEntries(new FormData(form).entries());
    try {
      const res = await api("/api/tracker/add", { method: "POST", body });
      toast(res.created ? `Added ${body.company}` : `${body.company} was already on your list`);
      form.reset();
      await loadTracker();
      loadJobs(); loadStats();
    } catch (err) {
      toast(`Could not add it: ${err.message}`);
    }
  });
  $("#trk-export").addEventListener("click", () => doExport("tracker"));
}

/* -------------------------------------------------------------------- export */
function registerExport(result) {
  if (result.error) { toast(result.error, 6000); return; }
  state.exports.unshift(result);
  $("#export-list").innerHTML = state.exports.map((e) =>
    `<div style="padding:6px 0;border-bottom:1px solid var(--line-soft)">
       <a href="/api/download/${encodeURIComponent(e.filename)}">${esc(e.filename)}</a>
       <span class="where"> - ${e.added} rows${
         e.skipped_duplicates ? `, ${e.skipped_duplicates} duplicates skipped` : ""}</span>
     </div>`).join("");
  toast(`Wrote ${result.added ?? 0} rows to ${result.filename}`);
}

let exportBusy = false;

async function doExport(mode) {
  if (exportBusy) { toast("Already building one - one moment."); return; }
  exportBusy = true;
  toast("Building workbook...");
  try {
    const result = await api("/api/export", {
      method: "POST", body: { mode, view: state.view },
    });
    if (result.error) { toast(result.error); return; }
    registerExport(result);
  } catch (err) {
    toast(`Export failed: ${err.message}`);
  } finally {
    exportBusy = false;
  }
}

/* ---------------------------------------------------------------------- wire */
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function init() {
  // Phone-width layout: the rail becomes an off-canvas panel behind the hamburger
  // button, closed by its own backdrop, the close button it does not otherwise have,
  // or picking a tab (there is nothing left to filter once you have navigated away
  // from Positions or Review).
  const closeRail = () => {
    $("#rail").classList.remove("open");
    $("#rail-backdrop").classList.remove("on");
    $("#btn-rail-toggle").setAttribute("aria-expanded", "false");
  };
  $("#btn-rail-toggle")?.addEventListener("click", () => {
    const open = $("#rail").classList.toggle("open");
    $("#rail-backdrop").classList.toggle("on", open);
    $("#btn-rail-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#rail-backdrop")?.addEventListener("click", closeRail);

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("on", t === tab));
    $$(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${tab.dataset.view}`));
    closeRail();
    if (tab.dataset.view === "tracker") { loadFollowups(); loadTracker(); }
  }));
  $$(".dtab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".dtab").forEach((t) => t.classList.toggle("on", t === tab));
    $$(".dpane").forEach((p) => p.classList.toggle("on", p.id === `pane-${tab.dataset.pane}`));
  }));
  $("#drawer-close").addEventListener("click", () => {
    $("#drawer").classList.remove("open");
    state.selected = null; renderRows();
  });

  const refresh = debounce(() => loadJobs(), 260);
  $("#btn-more").addEventListener("click", () => loadJobs(true));
  $("#f-q").addEventListener("input", (e) => { state.view.q = e.target.value; refresh(); });
  $("#f-score").addEventListener("input", (e) => {
    state.view.min_score = +e.target.value;
    $("#f-score-v").textContent = e.target.value;
    refresh();
  });
  ["status", "source", "order", "startups"].forEach((k) =>
    $(`#f-${k}`).addEventListener("change", (e) => { state.view[k] = e.target.value; loadJobs(); }));
  $("#f-star").addEventListener("change", (e) => { state.view.starred = e.target.checked; loadJobs(); });
  $("#f-remote").addEventListener("change", (e) => { state.view.remote = e.target.checked; loadJobs(); });
  $("#f-sponsored").addEventListener("change", (e) => {
    state.view.sponsored = e.target.checked; loadJobs();
  });
  $("#f-salary").addEventListener("change", (e) => {
    state.view.salaryDisclosed = e.target.checked; loadJobs();
  });
  $("#f-unclassified").addEventListener("change", (e) => {
    state.view.unclassified = e.target.checked; loadJobs();
  });
  $("#f-unviewed").addEventListener("change", (e) => {
    state.view.unviewed = e.target.checked; loadJobs();
  });
  $("#btn-reset").addEventListener("click", () => {
    Object.assign(state.view, { q: "", min_score: 0, status: "all", source: "all",
                                order: "score", starred: false, remote: false,
                                unviewed: false, startups: "any" });
    $("#f-q").value = ""; $("#f-score").value = 0; $("#f-score-v").textContent = "0";
    $("#f-status").value = "all"; $("#f-source").value = "all"; $("#f-order").value = "score";
    $("#f-star").checked = false; $("#f-remote").checked = false;
    $("#f-unviewed").checked = false; $("#f-startups").value = "any";
    $("#f-sponsored").checked = false; state.view.sponsored = false;
    $("#f-salary").checked = false; state.view.salaryDisclosed = false;
    state.view.employment = []; state.view.families = [];
    state.view.unclassified = false; $("#f-unclassified").checked = false;
    // The built-in facets above get redrawn from scratch by the next loadStats() call,
    // which clears their checkboxes automatically. The user's own facet rules live in
    // a different signature-cached renderer (profile.js), so they need telling too, or
    // "Reset filters" would silently leave one of your own rules ticked.
    state.view.rules = [];
    $$("#f-own input[type=checkbox]").forEach((box) => { box.checked = false; });
    loadStats();
    loadJobs();
  });

  $("#btn-scrape").addEventListener("click", async () => {
    try {
      const r = await api("/api/scrape", { method: "POST" });
      toast(r.started ? "Scrape started - this takes a few minutes"
                      : "A scrape is already running");
    } catch (err) {
      toast(`Could not start the scrape: ${err.message}`);
    }
  });

  $("#btn-export-new").addEventListener("click", () => doExport("new"));
  $("#btn-export-append").addEventListener("click", () => doExport("append"));
  $("#btn-t-new").addEventListener("click", () => doExport("new"));
  wireTracker();
  api("/api/tracker").then((d) => { trk.statuses = d.statuses; }).catch(() => {});
  $("#btn-t-append").addEventListener("click", () => doExport("append"));

  addTagInput("#c-title-add", "titles");
  addTagInput("#c-keyword-add", "keywords_any");
  addTagInput("#c-excluded-add", "keywords_excluded");
  addTagInput("#c-location-add", "locations");

  // Mirrors the server's own clamping (app.py's _num), so a stray "-30" in Maximum age
  // shows the value it will actually be saved as rather than round-tripping through a
  // save to find out the search silently went to zero results.
  const clampNum = (raw, low, high) => {
    const n = +raw;
    if (raw === "" || Number.isNaN(n)) return null;
    return Math.min(high, Math.max(low, n));
  };

  let savingCriteria = false;
  $("#btn-save-criteria").addEventListener("click", async () => {
    if (savingCriteria) return;
    savingCriteria = true;
    const btn = $("#btn-save-criteria");
    btn.disabled = true;
    const s = state.config.search;
    const weights = {};
    $$("#weights [data-w]").forEach((i) => { weights[i.dataset.w] = clampNum(i.value, 0, 100) ?? 0; });

    const maxAge = clampNum($("#c-maxage").value, 1, 3650);
    const salaryMin = clampNum($("#c-salary").value, 0, 1000000);
    const minScore = clampNum($("#c-minscore").value, 0, 100) ?? 0;
    if ($("#c-maxage").value !== "" && maxAge !== +$("#c-maxage").value) {
      $("#c-maxage").value = maxAge;
    }
    if ($("#c-minscore").value !== "" && minScore !== +$("#c-minscore").value) {
      $("#c-minscore").value = minScore;
    }

    $("#criteria-msg").textContent = "Re-scoring...";
    try {
      const r = await api("/api/config", {
        method: "POST",
        body: {
          search: {
            country: $("#c-country").value,
            career_stage: $("#c-stage").value,
            visa_sponsorship: $("#c-visa").value,
            work_modes: $$(".c-mode").filter((c) => c.checked).map((c) => c.value),
            titles: s.titles, keywords_any: s.keywords_any,
            keywords_excluded: s.keywords_excluded, locations: s.locations,
            remote_ok: $("#c-remote").checked, exclude_senior: $("#c-senior").checked,
            max_age_days: maxAge,
            salary_min: salaryMin,
          },
          min_score: minScore,
          weights,
        },
      });
      $("#criteria-msg").textContent =
        `Kept ${r.rescore.kept}, removed ${r.rescore.dropped}.`;
      // The country/currency hint under the selector, and the dropdown's own
      // "selected" option, are both derived from state.config.home - which a save
      // never updates on its own (the POST response is just {ok, rescore}). Without
      // this, changing country and saving leaves the old country's name showing
      // under a dropdown that now reads the new one, until the next full page load.
      await loadConfig();
      await loadJobs(); await loadStats();
    } catch (err) {
      $("#criteria-msg").textContent = "";
      toast(`Could not save: ${err.message}`);
    } finally {
      savingCriteria = false;
      btn.disabled = false;
    }
  });

  $("#chat-send").addEventListener("click", () => sendChat($("#chat-input").value));
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat(e.target.value);
  });
  $$(".suggest button").forEach((b) =>
    b.addEventListener("click", () => sendChat(b.textContent)));

  renderRows();   // the skeleton, shown instantly rather than an empty table
  restoreSavedView();   // loads config, restores the sidebar, then the list
  loadStats().then((s) => {
    $("#engine-note").textContent = "";
  });
  setInterval(loadStats, 4000);
}

document.addEventListener("DOMContentLoaded", init);
