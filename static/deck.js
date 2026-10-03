/* Review deck - swipe through the queue and save what is worth applying to.
 *
 * Swiping right SAVES. It does not apply, and there is no setting that makes it apply.
 * The saved posting goes onto the tracker with its apply link, and the panel beside the
 * deck says why it suits you and offers the interview brief. Applying stays a thing you
 * do, on the employer's own form, having read what you are sending.
 *
 * That is a deliberate limit, not a missing feature. An application cannot be recalled,
 * and a half-tailored one burns you with that employer for the season.
 *
 * Loaded after app.js and reuses its helpers ($, api, toast, esc, fmtDate, scoreClass,
 * loadStats, loadJobs, loadPrep, state).
 */

const deck = { cards: [], history: [], busy: false, reviewed: 0, shortlisted: 0 };

function paintCounts() {
  $("#deck-remaining").textContent = deck.cards.length;
  $("#deck-reviewed").textContent = deck.reviewed;
  $("#deck-shortlisted").textContent = deck.shortlisted;
  $("#deck-undo").disabled = deck.history.length === 0;
}

function cardHTML(j) {
  const sponsor = j.sponsor_name
    ? `<span class="pill sponsor">Sponsor ${esc(j.sponsor_rating || "")}</span>` : "";
  // Reflects what the posting's own text says, not just the raw flag the source
  // supplied - see app.js's renderRows for why that distinction matters.
  const workPill = j.work_mode === "remote" ? '<span class="pill remote">Remote</span>'
    : j.work_mode === "hybrid" ? '<span class="pill hybrid" title="Listed as Remote, '
      + 'but the posting reads as hybrid">Hybrid</span>'
    : j.work_mode === "onsite" && j.remote ? '<span class="pill hybrid" title="Listed '
      + 'as Remote, but the posting reads as on-site">On-site</span>' : "";
  const longOpen = j.long_open ? '<span class="pill caution" title="Open 45+ days - '
    + 'worth checking it is still live">Long-open</span>' : "";
  const direct = j.source_kind === "ats_direct"
    ? '<span class="pill direct">Direct</span>' : "";
  const salary = j.salary_display
    ? `<span class="pill">${esc(j.salary_display)}</span>` : "";
  const matched = (j.matched_skills || []).slice(0, 6)
    .map((s) => `<span class="chip">${esc(s)}</span>`).join("");
  const missing = (j.missing_skills || []).slice(0, 5)
    .map((s) => `<span class="chip gap">${esc(s)}</span>`).join("");

  return `<article class="card" data-id="${j.id}">
    <div class="stamp yes">Save</div>
    <div class="stamp no">Pass</div>
    <div class="role">${esc(j.title)}</div>
    <div class="org">${esc(j.company)} &middot; ${esc(j.location || "location not stated")}
         &middot; ${fmtDate(j.posted_at)}</div>
    <div class="meta">${direct}${workPill}${longOpen}${sponsor}${salary}</div>
    <div class="scoreline ${scoreClass(j.score)}">
      <span class="bigscore">${j.score.toFixed(0)}</span>
      <span class="bar" style="max-width:170px"><i style="width:${Math.min(100, j.score)}%"></i></span>
      <span class="where">${esc((j.score_reasons || [])[0] || "")}</span>
    </div>
    <div class="cardwhy" id="why-${esc(j.id)}">
      <div class="micro">Why this one</div>
      <div class="whytext">reading the advert…</div>
    </div>
    ${matched ? `<div style="margin-top:10px"><div class="micro">You have used</div>
      <div class="chips" style="margin-top:5px">${matched}</div></div>` : ""}
    ${missing ? `<div style="margin-top:8px"><div class="micro">Also asked for</div>
      <div class="chips" style="margin-top:5px">${missing}</div></div>` : ""}
    <div class="snippet">${esc((j.description || "").slice(0, 300))}...</div>
    <div class="cardmore">
      <button class="btn sm" data-more="${esc(j.id)}">See the full posting</button>
      <a class="btn sm" href="${esc(j.url || "#")}" target="_blank" rel="noopener">
        Open the advert</a>
    </div>
  </article>`;
}

function renderDeck() {
  const host = $("#deck");
  host.querySelectorAll(".card").forEach((c) => c.remove());
  $("#deck-empty").classList.toggle("on", deck.cards.length === 0);

  // Insert back-to-front so cards[0] ends up on top of the stack.
  deck.cards.slice(0, 3).reverse().forEach((j) => {
    host.insertAdjacentHTML("afterbegin", cardHTML(j));
  });
  const top = host.querySelector(".card");
  if (top) {
    top.classList.add("top");
    attachDrag(top);
    wireCardButtons(top);
    paintWhy(deck.cards[0]);
  }
  paintCounts();
}

async function loadDeck() {
  try {
    const data = await api(`/api/deck?limit=40&min_score=${state.view.min_score || 0}`);
    deck.cards = data.cards;
    deck.shortlisted = data.shortlisted;
    deck.reviewed = data.reviewed;
    renderDeck();
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------------------------------------------------------- gestures */
function attachDrag(el) {
  let startX = 0, startY = 0, dx = 0, dy = 0, active = false;

  el.addEventListener("pointerdown", (e) => {
    active = true; dx = 0; dy = 0;
    startX = e.clientX; startY = e.clientY;
    el.classList.add("dragging");
    try { el.setPointerCapture(e.pointerId); } catch (_) { /* not critical */ }
  });

  el.addEventListener("pointermove", (e) => {
    if (!active) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
    el.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 22}deg)`;
    el.querySelector(".stamp.yes").style.opacity = dx > 40 ? Math.min(1, dx / 130) : 0;
    el.querySelector(".stamp.no").style.opacity = dx < -40 ? Math.min(1, -dx / 130) : 0;
  });

  const release = () => {
    if (!active) return;
    active = false;
    el.classList.remove("dragging");
    if (dx > 110) { decide("shortlist"); return; }
    if (dx < -110) { decide("pass"); return; }
    if (dy < -110) { decide("star"); return; }
    el.classList.add("settle");
    el.style.transform = "";
    el.querySelectorAll(".stamp").forEach((s) => { s.style.opacity = 0; });
    setTimeout(() => el.classList.remove("settle"), 300);
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
}

function flyOut(el, direction) {
  const x = direction === "pass" ? -720 : direction === "shortlist" ? 720 : 0;
  const y = direction === "star" ? -720 : 60;
  el.classList.add("settle");
  el.style.transform = `translate(${x}px, ${y}px) rotate(${x / 26}deg)`;
  el.style.opacity = "0";
}

async function decide(decision) {
  if (deck.busy || !deck.cards.length) return;
  deck.busy = true;
  const job = deck.cards[0];
  const el = $("#deck .card.top");
  if (el) flyOut(el, decision);

  try {
    const res = await api(`/api/job/${job.id}/decide`, {
      method: "POST", body: { decision },
    });
    const wasShortlisted = decision === "shortlist" || decision === "star";
    deck.history.push({ job, previous: res.previous, wasShortlisted });
    deck.cards.shift();
    deck.reviewed += 1;
    if (wasShortlisted) deck.shortlisted += 1;
    setTimeout(() => { renderDeck(); deck.busy = false; }, 190);

    if (wasShortlisted) {
      showSaved(job);
    } else {
      $("#pack-host").innerHTML = "";
    }
    loadStats();
  } catch (err) {
    toast(err.message);
    deck.busy = false;
    renderDeck();
  }
}

async function undoDecision() {
  const last = deck.history.pop();
  if (!last) return;
  try {
    await api(`/api/job/${last.job.id}/decide`, {
      method: "POST", body: { decision: "reset" },
    });
    deck.cards.unshift(last.job);
    deck.reviewed = Math.max(0, deck.reviewed - 1);
    if (last.wasShortlisted) {
      deck.shortlisted = Math.max(0, deck.shortlisted - 1);
    }
    $("#pack-host").innerHTML = "";
    renderDeck();
    loadStats();
    toast(`Restored ${last.job.title}`);
  } catch (err) {
    toast(err.message);
  }
}

/* --------------------------------------------------- why this one suits you */
const whyCache = {};

async function paintWhy(job) {
  if (!job) return;
  const host = $(`#why-${job.id} .whytext`);
  if (!host) return;
  if (whyCache[job.id]) { renderWhy(host, whyCache[job.id]); return; }
  try {
    const data = await api(`/api/job/${job.id}/why`);
    whyCache[job.id] = data;
    // The user may have swiped on while this was in flight.
    const live = $(`#why-${job.id} .whytext`);
    if (live) renderWhy(live, data);
  } catch (err) {
    host.textContent = "Could not work out a reason for this one.";
  }
}

function renderWhy(host, data) {
  const conf = (data.confidence || "").split(" ")[0];
  host.innerHTML = `${esc(data.why)}
    ${data.against ? `<div class="against"><b>Against:</b> ${esc(data.against)}</div>` : ""}
    <div class="whyfoot">
      <span class="conf ${esc(conf)}">${esc(data.confidence)} confidence</span>
      <span class="where">${esc(data.engine)}</span>
    </div>`;
}

function wireCardButtons(card) {
  const more = card.querySelector("button[data-more]");
  if (more) {
    more.addEventListener("click", (e) => {
      e.stopPropagation();
      openDrawer(more.dataset.more);
    });
  }
  // A drag that starts on a button or a link should not throw the card across the room.
  card.querySelectorAll("button, a").forEach((el) => {
    el.addEventListener("pointerdown", (e) => e.stopPropagation());
  });
}

/* ------------------------------------------------------------ saved posting */
async function showSaved(job) {
  const host = $("#pack-host");
  host.innerHTML = `<div class="pack">
    <header><b>Saved &mdash; ${esc(job.title)}</b>
      <span class="micro">${esc(job.company)}</span></header>
    <div class="body">
      <div class="savedline">
        On your tracker. Nothing has been sent to ${esc(job.company)} and nothing will be.
      </div>
      <div class="rowbtns">
        <a class="btn sm primary" href="${esc(job.url || "#")}" target="_blank"
           rel="noopener">Open their application form</a>
        <button class="btn sm" id="saved-prep">Prepare for the interview</button>
        <button class="btn sm" id="saved-letter">Draft a cover letter</button>
        <button class="btn sm" id="saved-review">Review my CV for this role</button>
        <button class="btn sm" id="saved-applied">I have applied</button>
        <button class="btn sm" id="saved-export">Add to my tracker now</button>
      </div>
      <div class="savedwhy" id="saved-why"></div>
      <div id="saved-letter-host"></div>
      <div id="saved-review-host"></div>
      <div id="saved-prep-host"></div>
      <div class="where tight" style="margin-top:14px">
        Saved postings export to the tracker with the company, role, deadline, location,
        salary and apply link already filled in. The apply and progress columns stay
        yours.
      </div>
    </div></div>`;

  const cached = whyCache[job.id];
  if (cached) {
    $("#saved-why").innerHTML = '<div class="micro">Why you saved it</div>';
    const holder = document.createElement("div");
    holder.className = "whytext";
    $("#saved-why").appendChild(holder);
    renderWhy(holder, cached);
  }

  $("#saved-prep").addEventListener("click", () => {
    loadPrep(job.id, "#saved-prep-host");
  });
  $("#saved-letter").addEventListener("click", () => showLetter(job));
  $("#saved-review").addEventListener("click", () => showReview(job));
  $("#saved-applied").addEventListener("click", async () => {
    await api(`/api/job/${job.id}/status`, {
      method: "POST", body: { status: "applied" },
    });
    toast(`Marked applied: ${job.title}`);
    loadStats(); loadJobs();
  });
  $("#saved-export").addEventListener("click", async () => {
    const res = await api("/api/export", {
      method: "POST", body: { mode: "new", job_ids: [job.id] },
    });
    if (res.error) { toast(res.error); return; }
    registerExport(res);
    toast(`Tracker written: ${res.filename}`);
  });
}

/* ------------------------------------------------------------ cover letter */
async function showLetter(job) {
  const host = $("#saved-letter-host");
  host.innerHTML = skeletonReport("Drafting a letter grounded in this posting and your own skills…");
  let pack;
  try {
    pack = await api(`/api/job/${job.id}/pack`);
  } catch (err) {
    host.innerHTML = `<div class="warn">${esc(err.message)}</div>`;
    return;
  }

  const checks = pack.checklist.map((c) => `<div class="checkrow">
      <span class="mark ${c.ready ? "ok" : "no"}">${c.ready ? "\u2713" : "!"}</span>
      <span class="what">${esc(c.item)}</span>
      <span class="note">${esc(c.note)}</span></div>`).join("");
  const questions = pack.questions.map((q) => `<div class="checkrow">
      <span class="mark"></span>
      <span class="what">${esc(q.question)}</span>
      <span class="note">${esc(q.note)}</span></div>`).join("");
  const warnings = (pack.warnings || [])
    .map((w) => `<div class="warn">${esc(w)}</div>`).join("");

  host.innerHTML = `<div class="block">
    ${warnings}
    <div class="micro">Draft &mdash; by ${esc(pack.engine)}. Edit the bracketed parts,
      and sign it yourself.</div>
    <textarea class="letter" id="pack-letter">${esc(pack.cover_letter)}</textarea>
    <div class="rowbtns" style="margin:10px 0 14px">
      <button class="btn sm" id="pack-copy">Copy</button>
    </div>
    <div class="micro">Before you submit</div>
    ${checks}
    <div class="micro" style="margin-top:14px">The form will probably ask</div>
    ${questions}
    <div class="where tight" style="margin-top:12px">
      Job Scout does not submit applications. It drafts, and opens the employer's own
      form, so what gets sent is something you have read.
    </div>
  </div>`;

  $("#pack-copy").addEventListener("click", () => {
    navigator.clipboard.writeText($("#pack-letter").value);
    toast("Cover letter copied");
  });
}

/* -------------------------------------------------------------- CV review */
async function showReview(job) {
  const host = $("#saved-review-host");
  host.innerHTML = skeletonReport("Reading your CV against this posting…");
  let r;
  try {
    r = await api(`/api/job/${job.id}/review`, { method: "POST", body: {} });
  } catch (err) {
    host.innerHTML = `<div class="warn">${esc(err.message)}</div>`;
    return;
  }

  const caveats = (r.caveats || [])
    .map((c) => `<div class="warn soft">${esc(c)}</div>`).join("");
  const matched = (r.matched_skills || [])
    .map((s) => `<span class="chip">${esc(s)}</span>`).join("");
  const missing = (r.missing_skills || []).map((m) => `<div class="prow">
      <div class="pk">${esc(m.skill)}</div>
      <div class="pv">${esc(m.advice)}</div></div>`).join("");
  const structure = (r.structure || []).map((c) => `<div class="checkrow">
      <span class="mark ${c.ok ? "ok" : "no"}">${c.ok ? "✓" : "!"}</span>
      <span class="what">${esc(c.check)}</span>
      <span class="note">${esc(c.note)}</span></div>`).join("");
  const tellsConf = r.voice_score >= 70 ? "high" : r.voice_score >= 40 ? "moderate" : "low";
  const tells = (r.voice_tells || []).map((t) => `<div class="checkrow">
      <span class="mark"></span>
      <span class="what">${esc(t.phrase)} &rarr; ${esc(t.instead)}</span>
      <span class="note">${esc(t.why)}</span></div>`).join("");
  const notes = (r.content_notes || []).map((n) => `<div class="prow">
      <div class="pk">${esc(n.source)}</div>
      <div class="pv">${esc(n.note)}
        <div class="where" style="margin-top:3px">&ldquo;${esc(n.evidence)}&rdquo;</div>
      </div></div>`).join("");
  const resources = (r.resources || []).map((res) => `<li>
      <a href="${esc(res.url)}" target="_blank" rel="noopener">${esc(res.name)}</a>
      <div class="where">${esc(res.what)}</div></li>`).join("");

  host.innerHTML = `<div class="block">
    ${caveats}
    ${matched || missing ? `
      <div class="micro">Skills this role asks for that you have</div>
      <div class="chips" style="margin:4px 0 10px">${matched
        || '<span class="where">None matched directly - see the gaps below.</span>'}</div>
      ${missing ? `<div class="micro">Asked for, not showing up yet</div>${missing}` : ""}
    ` : ""}
    ${structure ? `
      <div class="micro" style="margin-top:14px">CV structure</div>${structure}` : ""}
    ${r.cover_letter_checked ? `
      <div class="micro" style="margin-top:14px">Cover letter voice
        <span class="conf ${tellsConf}">${r.voice_score}/100</span></div>
      ${tells || '<div class="where" style="margin-top:4px">Reads like something a '
        + 'person actually wrote.</div>'}` : ""}
    ${notes ? `
      <div class="micro" style="margin-top:14px">Specific to this job
        <span class="where">(by ${esc(r.engine)}, every note checked against the
        actual text below it)</span></div>${notes}` : ""}
    <div class="micro" style="margin-top:14px">Where to learn more</div>
    <ul class="links">${resources}</ul>
    <div class="where tight" style="margin-top:12px">
      Nothing here comes from Glassdoor or other applicants - there is no honest source
      for either. This is built from the posting's own text, your own skills, and real
      published CV guidance.
    </div>
  </div>`;
}

/* ----------------------------------------------------------------- wiring */
function initDeck() {
  $("#btn-pass").addEventListener("click", () => decide("pass"));
  $("#btn-star").addEventListener("click", () => decide("star"));
  $("#btn-short").addEventListener("click", () => decide("shortlist"));
  $("#deck-undo").addEventListener("click", undoDecision);

  // Load the queue whenever the Review tab is opened.
  $$(".tab").forEach((tab) => {
    if (tab.dataset.view === "review") {
      tab.addEventListener("click", loadDeck);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (!$("#view-review").classList.contains("on")) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); decide("pass"); }
    if (e.key === "ArrowRight") { e.preventDefault(); decide("shortlist"); }
    if (e.key === "ArrowUp") { e.preventDefault(); decide("star"); }
    if (e.key.toLowerCase() === "u") { e.preventDefault(); undoDecision(); }
  });
}

document.addEventListener("DOMContentLoaded", initDeck);
