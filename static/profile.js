/* Profile tab: sign in, link LinkedIn, your own skills and filters, and a plain
 * account of what is stored.
 *
 * The rule this file exists to make visible: nothing about the user is typed in, and
 * nothing inferred is applied until they press a button. So every screen here shows
 * what was read, what was ignored, and what would change - then waits.
 *
 * Loaded after app.js and deck.js; reuses $, $$, api, toast, esc, state, loadJobs,
 * loadStats.
 */

const prof = { account: null, profile: null, suggestions: null, rules: [], counts: {} };

/* ------------------------------------------------------------------ account */
function accountHTML(a) {
  if (a.signed_in) {
    return `<div class="signedin">
      <div>
        <div class="who-big">${esc(a.username)}</div>
        <div class="where">Signed in${a.has_profile ? ", profile loaded" : ""}.
          Account created ${esc((a.since || "").slice(0, 10))}.</div>
      </div>
      <div class="rowbtns">
        <button class="btn sm" id="btn-signout">Sign out</button>
        <button class="btn sm" id="btn-changepw">Change password</button>
        <button class="btn sm danger" id="btn-delacct">Delete account</button>
      </div>
    </div>
    <div class="kept">
      <div class="micro">All this account holds</div>
      <ul>${a.what_is_kept.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>
      <div class="where">${esc(a.note)}</div>
    </div>`;
  }
  return `<div class="signin">
    <div class="field">
      <label class="micro">Username &mdash; invent one, not an email</label>
      <input type="text" id="a-user" autocomplete="username" placeholder="e.g. shwe">
    </div>
    <div class="field">
      <label class="micro">Password</label>
      <input type="password" id="a-pass" autocomplete="current-password"
             placeholder="at least 8 characters">
    </div>
    <div class="rowbtns">
      <button class="btn primary" id="btn-signin">Sign in</button>
      <button class="btn" id="btn-signup">Create an account</button>
    </div>
    <div class="where" style="margin-top:10px;line-height:1.6">
      ${esc(a.note)}
      ${a.known_usernames.length
        ? `<br>On this machine already: ${a.known_usernames.map(esc).join(", ")}.`
        : ""}
    </div>
  </div>`;
}

async function loadAccount() {
  prof.account = await api("/api/account");
  $("#account-state").innerHTML = accountHTML(prof.account);
  wireAccount();
}

function wireAccount() {
  const a = prof.account;
  if (!a.signed_in) {
    const submit = async (path) => {
      const username = $("#a-user").value.trim();
      const password = $("#a-pass").value;
      try {
        const res = await api(path, { method: "POST", body: { username, password } });
        toast(`Signed in as ${res.username}`);
        await refreshProfileTab();
        // Each account keeps its own sidebar filters; show this one's, not the last.
        await restoreSavedView(); loadStats();
      } catch (err) { toast(readError(err)); }
    };
    $("#btn-signin").addEventListener("click", () => submit("/api/account/login"));
    $("#btn-signup").addEventListener("click", () => submit("/api/account/create"));
    $("#a-pass").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit("/api/account/login");
    });
    return;
  }

  $("#btn-signout").addEventListener("click", async () => {
    await api("/api/account/logout", { method: "POST" });
    toast("Signed out. Your profile stays on this machine.");
    await refreshProfileTab();
    await restoreSavedView(); loadStats();
  });

  $("#btn-changepw").addEventListener("click", async () => {
    const current = window.prompt("Current password");
    if (current === null) return;
    const next = window.prompt("New password (at least 8 characters)");
    if (!next) return;
    try {
      await api("/api/account/password", { method: "POST", body: { current, new: next } });
      toast("Password changed.");
    } catch (err) { toast(readError(err)); }
  });

  $("#btn-delacct").addEventListener("click", async () => {
    // Irreversible and there is no reset by email, so it asks twice and says what goes.
    if (!window.confirm(
      "Delete this account?\n\nThis removes your login, your derived profile and your "
      + "saved criteria. It cannot be undone, and there is no password reset by email.\n\n"
      + "Your saved jobs and notes are not touched.")) return;
    const password = window.prompt("Type your password to confirm");
    if (!password) return;
    try {
      const res = await api("/api/account/delete", { method: "POST", body: { password } });
      toast(`Deleted: ${res.deleted.join(", ")}`);
      await refreshProfileTab();
    } catch (err) { toast(readError(err)); }
  });
}

// Flask sends {"error": "..."} with a non-2xx code; api() throws "400 {json}".
function readError(err) {
  const match = /\{[\s\S]*\}/.exec(err.message || "");
  if (match) {
    try { return JSON.parse(match[0]).error || err.message; } catch (_) { /* fall back */ }
  }
  return err.message;
}

/* ------------------------------------------------------------------- import */
function importGuideHTML(how) {
  return `<div class="routes">
    <div class="route best">
      <div class="tag">Best</div>
      <b>${esc(how.best.what)}</b>
      <p>${esc(how.best.where)}</p>
      <div class="where">${esc(how.best.why)}</div>
    </div>
    <div class="route">
      <div class="tag">Quicker</div>
      <b>${esc(how.quicker.what)}</b>
      <p>${esc(how.quicker.where)}</p>
      <div class="where">${esc(how.quicker.why)}</div>
    </div>
  </div>
  <div class="note-plain">${esc(how.why_not_a_url)}</div>`;
}

function suggestionsHTML(s) {
  if (!s) return "";
  const chips = (items) => (items || [])
    .map((t) => `<span class="chip">${esc(t)}</span>`).join("");
  return `<div class="suggestbox">
    <div class="micro">What it read, and what it would change</div>
    <ul class="why">${(s.why || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    <div class="grid2" style="margin-top:10px">
      <div><div class="micro">Job titles to search</div>
        <div class="chips">${chips(s.titles)}</div></div>
      <div><div class="micro">Skills to match on</div>
        <div class="chips">${chips(s.keywords_any)}</div></div>
    </div>
    <div class="rowbtns" style="margin-top:14px">
      <button class="btn primary" id="btn-apply-sug">Use these criteria</button>
      <button class="btn" id="btn-skip-sug">Leave my criteria alone</button>
    </div>
    <div class="where" style="margin-top:8px">
      Nothing above has been applied. Using them <b>adds</b> to what you already have
      rather than replacing it, and you can edit every one on the Criteria tab.
    </div>
  </div>`;
}

async function doImport(body) {
  $("#import-result").innerHTML = skeletonReport("Reading your file…");
  try {
    const res = await api("/api/profile/import", { method: "POST", body });
    prof.profile = res.profile;
    prof.suggestions = res.suggestions;
    $("#import-result").innerHTML =
      `<div class="ok-line">${esc(res.note)}</div>` + suggestionsHTML(res.suggestions);
    wireSuggestions();
    // Re-fetch everything rather than patch pieces in place: a new import can change
    // the school, the field of study and therefore who is hiring in it, and re-running
    // the one function that already knows how to load all of that consistently is
    // simpler than trying to keep two code paths in sync.
    await refreshProfileTab();
    loadStats();
  } catch (err) {
    $("#import-result").innerHTML = `<div class="warn">${esc(readError(err))}</div>`;
  }
}

function wireSuggestions() {
  const apply = $("#btn-apply-sug");
  if (!apply) return;
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      const res = await api("/api/profile/apply", { method: "POST", body: {} });
      const r = res.rescore || {};
      toast(`Criteria updated. ${r.kept ?? 0} postings match now.`);
      $("#import-result").innerHTML =
        `<div class="ok-line">Applied. ${esc(Object.keys(res.applied).join(", "))}
         updated &mdash; ${r.kept ?? 0} postings match, ${r.restored ?? 0} came back.</div>`;
      loadJobs(); loadStats();
      if (typeof loadConfig === "function") loadConfig();
    } catch (err) { toast(readError(err)); apply.disabled = false; }
  });
  $("#btn-skip-sug").addEventListener("click", () => {
    $("#import-result").innerHTML =
      '<div class="where">Left alone. Your profile is still used for match scores.</div>';
  });
}

/* ------------------------------------------------------------------ profile */
function renderProfile() {
  const host = $("#profile-body");
  const p = prof.profile;
  if (!p) {
    host.innerHTML = `<div class="where">
      Nothing yet. Link your LinkedIn above, or point
      <code>profile.cv_path</code> at a CV in <code>config.yaml</code>.
      Without either, postings are matched on your search terms alone.</div>`;
    return;
  }
  const row = (label, value) => value
    ? `<div class="pfrow"><span class="k">${esc(label)}</span>
         <span class="v">${esc(value)}</span></div>` : "";
  const chips = (items) => (items || [])
    .map((t) => `<span class="chip">${esc(t)}</span>`).join("");
  // Skills are also buttons: click one to add it as a search keyword right there,
  // instead of only ever being offered once as an all-or-nothing import suggestion.
  const skillChips = (items) => (items || []).map((t) => {
    const already = (state.config?.search?.keywords_any || [])
      .some((k) => k.toLowerCase() === t.toLowerCase());
    return `<button type="button" class="chip skillchip ${already ? "on" : ""}"
        data-skill="${esc(t)}" title="${already ? "Already a search keyword"
          : "Click to add as a search keyword"}">${esc(t)}${already ? " ✓" : ""}</button>`;
  }).join("");

  host.innerHTML = `
    <div class="pfgrid">
      ${row("Education", [p.degree, p.field_of_study].filter(Boolean).join(" in "))}
      ${row("Level", (p.education_level || "").replace(/_/g, " "))}
      ${row("Institution", p.institution)}
      ${row("Finishing", p.graduation_year)}
      ${row("Experience", p.years_experience ? `${p.years_experience} years` : "")}
      ${row("Region", p.region)}
      ${row("Languages", (p.languages || []).join(", "))}
      ${row("Read from", (p.source || "").replace(/_/g, " "))}
    </div>
    ${(p.recent_titles || []).length ? `<div style="margin-top:12px">
      <div class="micro">Roles you have held</div>
      <div class="chips">${chips(p.recent_titles)}</div></div>` : ""}
    <div style="margin-top:12px">
      <div class="micro">Skills matched against every posting (${(p.skills || []).length})</div>
      <div class="chips">${skillChips(p.skills)}</div>
      <div class="where" style="margin-top:5px">
        Click a skill to add it as a search keyword. A tick means it already is one.
      </div>
    </div>
    <div class="twocol" style="margin-top:16px">
      <div>
        <div class="micro good">Kept</div>
        <div class="where tight">${(prof.kept || []).map(esc).join(", ")}</div>
      </div>
      <div>
        <div class="micro bad">Never kept, even when handed over</div>
        <div class="where tight">${(prof.never || []).map((k) => esc(k.replace(/_/g, " "))).join(", ")}</div>
      </div>
    </div>
    <div class="rowbtns" style="margin-top:14px">
      <button class="btn sm danger" id="btn-forget">Forget my profile</button>
    </div>`;

  host.querySelectorAll("button[data-skill]").forEach((btn) => {
    btn.addEventListener("click", () => addSkillAsKeyword(btn.dataset.skill, btn));
  });

  $("#btn-forget").addEventListener("click", async () => {
    if (!window.confirm("Delete the profile read from your LinkedIn?\n\n"
      + "Your criteria, saved jobs and notes stay.")) return;
    await api("/api/profile/forget", { method: "POST" });
    prof.profile = null; prof.suggestions = null;
    renderProfile();
    toast("Profile deleted.");
    loadStats();
  });
}

async function addSkillAsKeyword(skill, btn) {
  if (btn.classList.contains("on")) return;
  btn.disabled = true;
  try {
    if (!state.config) state.config = await api("/api/config");
    const existing = state.config.search.keywords_any || [];
    if (existing.some((k) => k.toLowerCase() === skill.toLowerCase())) {
      btn.classList.add("on"); btn.textContent = `${skill} ✓`;
      return;
    }
    const keywords_any = existing.concat([skill]);
    const r = await api("/api/config", { method: "POST", body: { search: { keywords_any } } });
    state.config.search.keywords_any = keywords_any;
    btn.classList.add("on"); btn.textContent = `${skill} ✓`;
    const rr = r.rescore || {};
    toast(`Added "${skill}" as a search keyword. ${rr.kept ?? 0} postings match now.`);
    loadJobs(); loadStats();
  } catch (err) {
    toast(readError(err));
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------ profile review */
async function renderProfileReview() {
  const host = $("#profile-review-body");
  if (!host) return;
  try {
    const data = await api("/api/profile/review");
    const review = data.review;
    if (!review) {
      host.innerHTML = `<div class="where">${esc(data.note
        || "Nothing yet. Link your LinkedIn above first.")}</div>`;
      return;
    }
    const off = review.official;
    const tone = off.present_count >= 6 ? "s-hi" : off.present_count >= 4 ? "s-mid" : "s-low";

    const statusBadge = (present) => present === true
      ? '<span class="badge boost">present</span>'
      : present === false
        ? '<span class="badge exclude">missing</span>'
        : '<span class="badge facet">can&rsquo;t check</span>';

    const sectionRows = off.sections.map((s) => `<div class="prow">
        <div class="pk">${esc(s.name)}</div>
        <div class="pv">${statusBadge(s.present)}
          <div class="where" style="margin-top:3px">${esc(s.why)}</div>
        </div>
      </div>`).join("");

    const tipRows = (review.quality_tips || []).map((t) => `<div class="prow">
        <div class="pk">${esc(t.label)}</div>
        <div class="pv">${esc(t.detail)}</div>
      </div>`).join("");

    host.innerHTML = `
      <div class="scorecell ${tone}" style="margin-bottom:8px">
        <span class="scoreval">${off.present_count} / ${off.total}</span>
        <div class="bar"><i style="width:${Math.round(
          (off.present_count / off.total) * 100)}%"></i></div>
      </div>
      <div class="where" style="margin-bottom:14px">${esc(off.summary_line)}</div>
      <div class="micro">LinkedIn's own 7-section checklist</div>
      ${sectionRows}
      <div class="where" style="margin:10px 0 16px">${esc(off.source)}</div>
      <div class="micro">Job Scout's own quality checks &mdash; not from LinkedIn</div>
      ${tipRows || '<div class="where">Nothing extra to check yet.</div>'}
      <div class="where" style="margin-top:12px">${esc(review.note)}</div>`;
  } catch (err) {
    host.innerHTML = `<div class="warn">${esc(err.message)}</div>`;
  }
}

/* --------------------------------------------------------- who's hiring in it */
async function renderEmployers() {
  const host = $("#employers-body");
  if (!host) return;
  try {
    const data = await api("/api/profile/employers");
    if (!data.families.length) {
      host.innerHTML = `<div class="where">${esc(data.note || "Nothing to show yet.")}</div>`;
      return;
    }
    const familyLabel = (key) => {
      const found = (state.vocabulary?.families || []).find((f) => f.key === key);
      return found ? found.label : key.replace(/_/g, " ");
    };
    const rows = data.employers.length
      ? data.employers.map((e) => `<div class="prow">
          <div class="pk">${esc(e.company)}</div>
          <div class="pv">${e.postings} posting${e.postings === 1 ? "" : "s"} on file
            <span class="where">${e.families.map(familyLabel).join(", ")}</span></div>
        </div>`).join("")
      : '<div class="where">None of the employers in the database have posted an '
        + 'early-career role in this field yet &mdash; worth running a fresh scrape.</div>';
    host.innerHTML = `
      <div class="where" style="margin-bottom:10px">
        Based on ${esc(data.families.map(familyLabel).join(", "))}${data.kinds
          ? ", internships, placements, graduate schemes and apprenticeships only"
          : ""}. Counted from postings this install has actually found &mdash;
        not LinkedIn alumni data, which nobody publishes an API for.
      </div>
      ${rows}
      ${prof.schoolAlumniUrl ? `<div class="rowbtns" style="margin-top:14px">
        <a class="btn sm" href="${esc(prof.schoolAlumniUrl)}" target="_blank"
           rel="noopener">Browse ${esc(prof.school)}'s alumni on LinkedIn</a>
      </div>
      <div class="where" style="margin-top:6px">
        That link is LinkedIn's own view of where your school's alumni work &mdash;
        opens in your own logged-in session. This app cannot fetch it for you; only
        LinkedIn can show it, and only to someone signed in.
      </div>` : ""}`;
  } catch (err) {
    host.innerHTML = `<div class="warn">${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------- your skills */
function renderSkills(skills) {
  const host = $("#p-skills");
  host.innerHTML = (skills || []).length
    ? skills.map((s) => `<span class="tag">${esc(s)}<button type="button"
        data-skill="${esc(s)}" title="Remove">&times;</button></span>`).join("")
    : '<span class="where">None added yet.</span>';
  host.querySelectorAll("button[data-skill]").forEach((el) => {
    el.addEventListener("click", () => saveSkills({ remove: [el.dataset.skill] }));
  });
}

async function saveSkills(body) {
  const res = await api("/api/profile/skills", { method: "POST", body });
  renderSkills(res.extra_skills);
  loadJobs(); loadStats();
}

/* ------------------------------------------------------------- your filters */
const MODE_LABEL = { exclude: "Never show", require: "Only show", boost: "Rank higher" };

function renderRules() {
  const host = $("#rules-list");
  if (!prof.rules.length) {
    host.innerHTML = '<div class="where">No filters of your own yet.</div>';
  } else {
    host.innerHTML = prof.rules.map((r) => `<div class="rulerow" data-key="${esc(r.key)}">
      <div class="rl">
        <b>${esc(r.label)}</b>
        <span class="badge ${esc(r.mode)}">${esc(MODE_LABEL[r.mode] || r.mode)}</span>
        ${r.facet ? '<span class="badge facet">in the sidebar</span>' : ""}
        <div class="where">${esc(r.terms.join(", "))} &mdash; ${esc(r.field)}
          ${r.mode === "boost" ? `&mdash; ${r.weight > 0 ? "+" : ""}${r.weight} points` : ""}</div>
      </div>
      <div class="rr">
        <span class="count">${prof.counts[r.key] ?? 0}</span>
        <button class="btn sm danger" data-del="${esc(r.key)}">Remove</button>
      </div>
    </div>`).join("");
    host.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.addEventListener("click", () => {
        saveRules(prof.rules.filter((r) => r.key !== btn.dataset.del));
      });
    });
  }
  renderOwnFacets();
}

// The custom facets in the left rail, alongside Job type and Field of work.
function renderOwnFacets() {
  const facets = prof.rules.filter((r) => r.facet);
  const field = $("#own-facets-field");
  const host = $("#f-own");
  if (!facets.length) { field.style.display = "none"; return; }
  field.style.display = "";
  // The listing's own numbers (narrowed by every other filter) once it has loaded;
  // the whole-database count only until then.
  const counts = (state.facets && state.facets.own_rules) || prof.counts;
  host.innerHTML = facets.map((r) => `<label>
      <input type="checkbox" value="${esc(r.key)}"
             ${(state.view.rules || []).includes(r.key) ? "checked" : ""}>
      <span>${esc(r.label)}</span><span class="n">${counts[r.key] ?? 0}</span>
    </label>`).join("");
  host.querySelectorAll("input").forEach((box) => {
    box.addEventListener("change", () => {
      state.view.rules = Array.from(host.querySelectorAll("input:checked"))
        .map((b) => b.value);
      loadJobs();
    });
  });
}

async function saveRules(rules) {
  try {
    const res = await api("/api/rules", { method: "POST", body: { rules } });
    prof.rules = res.rules;
    prof.counts = res.counts;
    renderRules();
    const r = res.rescore || {};
    toast(`Filters saved. ${r.kept ?? 0} postings match, ${r.restored ?? 0} came back.`);
    loadJobs(); loadStats();
  } catch (err) { toast(readError(err)); }
}

function wireRuleForm() {
  $("#btn-rule-add").addEventListener("click", () => {
    const label = $("#r-label").value.trim();
    const terms = $("#r-terms").value.split(",").map((t) => t.trim()).filter(Boolean);
    if (!label || !terms.length) {
      toast("A filter needs a name and at least one word.");
      return;
    }
    const mode = $("#r-mode").value;
    saveRules(prof.rules.concat([{
      label, terms, mode, field: $("#r-field").value,
      weight: mode === "boost" ? 10 : 0,
      // A "never show" rule that is also a checkbox is contradictory: the postings it
      // hides are gone, so the box would always read zero.
      facet: mode === "boost" && $("#r-facet").checked,
    }]));
    $("#r-label").value = ""; $("#r-terms").value = "";
  });
  $("#r-mode").addEventListener("change", () => {
    const boost = $("#r-mode").value === "boost";
    $("#r-facet").disabled = !boost;
    $("#r-facet").checked = boost;
  });
}

/* ------------------------------------------------------------------ privacy */
const kb = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n > 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

function renderPrivacy(data) {
  $("#privacy-body").innerHTML = `
    <ul class="principles">
      ${data.principles.map((p) => `<li>${esc(p)}</li>`).join("")}
    </ul>
    <table class="inv">
      <thead><tr><th>What</th><th>Where</th><th>Size</th><th></th></tr></thead>
      <tbody>${data.inventory.map((row) => `<tr class="${row.personal ? "personal" : ""}">
        <td><b>${esc(row.what)}</b><div class="where">${esc(row.detail)}</div></td>
        <td><code>${esc(row.where)}</code></td>
        <td>${kb(row.bytes)}</td>
        <td>${purgeButton(row.what)}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  $$("#privacy-body button[data-purge]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm(`Delete: ${btn.dataset.purge}? This cannot be undone.`)) return;
      const res = await api("/api/privacy/purge",
        { method: "POST", body: { target: btn.dataset.purge } });
      toast(`Removed ${res.removed}.`);
      refreshProfileTab();
    });
  });
}

function purgeButton(what) {
  const target = { "Your derived profile": "profile", "Page cache": "cache" }[what];
  if (!target) return "";
  return `<button class="btn sm danger" data-purge="${target}">Delete</button>`;
}

/* ------------------------------------------------------------------ wiring */
async function refreshProfileTab() {
  const [account, profile, rules, privacyData] = await Promise.all([
    api("/api/account"), api("/api/profile"), api("/api/rules"), api("/api/privacy"),
  ]);
  prof.account = account;
  prof.profile = profile.profile;
  prof.suggestions = profile.suggestions;
  prof.kept = profile.kept;
  prof.never = profile.never_kept;
  prof.rules = rules.rules;
  prof.counts = rules.counts;
  prof.school = profile.school;
  prof.schoolAlumniUrl = profile.school_alumni_url;

  $("#account-state").innerHTML = accountHTML(account);
  wireAccount();
  $("#import-guide").innerHTML = importGuideHTML(profile.how_to_import);
  renderProfile();
  renderProfileReview();
  renderEmployers();
  renderSkills(profile.extra_skills);
  renderRules();
  renderPrivacy(privacyData);
  if (prof.suggestions && !$("#import-result").innerHTML.trim()) {
    $("#import-result").innerHTML = suggestionsHTML(prof.suggestions);
    wireSuggestions();
  }
}

function initProfile() {
  $("#btn-import").addEventListener("click",
    () => doImport({ path: $("#p-path").value }));
  $("#btn-import-text").addEventListener("click",
    () => doImport({ text: $("#p-text").value }));
  $("#p-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doImport({ path: $("#p-path").value });
  });
  $("#p-skill-add").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.value.trim()) return;
    saveSkills({ add: [e.target.value.trim()] });
    e.target.value = "";
  });
  wireRuleForm();

  $("#btn-add-filter").addEventListener("click", () => {
    const tab = $$(".tab").find((t) => t.dataset.view === "profile");
    if (tab) tab.click();
    setTimeout(() => {
      $("#rule-new").scrollIntoView({ behavior: "smooth", block: "center" });
      $("#r-label").focus();
    }, 60);
  });

  $$(".tab").forEach((tab) => {
    if (tab.dataset.view === "profile") tab.addEventListener("click", refreshProfileTab);
  });

  // The rail's custom facets must exist before the Profile tab is ever opened.
  refreshProfileTab().catch((err) => console.warn("profile tab:", err));
}

document.addEventListener("DOMContentLoaded", initProfile);
