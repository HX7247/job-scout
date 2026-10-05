"""Score a job against the search criteria and the CV profile.

Output is 0-100 plus a human-readable reason list, so every ranking decision the UI
shows can be justified back to the user.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from rapidfuzz import fuzz

from functools import lru_cache

from . import classify, geo, rules, sponsorship
from .config import Config
from .cv import CVProfile, extract_skills
from .models import Job

# Early-career signals - the difference between a placement and a senior role.
EARLY_CAREER = [
    # English
    "placement", "year in industry", "industrial placement", "intern", "internship",
    "graduate", "apprentice", "trainee", "entry level", "entry-level", "junior",
    "summer analyst", "student", "sandwich", "12 month", "12-month", "undergraduate",
    "campus", "co-op", "co op", "new grad", "early career", "vacation scheme",
    "school leaver", "fresher", "associate programme", "rotational program",
    # other markets - postings are often written in the local language
    "praktikum", "praktikant", "werkstudent", "ausbildung", "einsteiger",   # DE
    "stage", "stagiaire", "alternance", "alternant", "apprenti",            # FR
    "becario", "practicas", "prácticas", "aprendiz",                        # ES
    "estagio", "estágio", "estagiário", "trainee program",                  # BR/PT
    "tirocinio", "stagista",                                                # IT
    "praktijk", "starter", "afstudeer",                                     # NL
    "praktyki", "staż", "stazysta",                                         # PL
]
SENIOR_SIGNALS = [
    "senior", "staff ", "principal", "lead ", "head of", "director", "vp ",
    "vice president", "manager", "chief", "architect", "10+ years", "8+ years",
    "7+ years", "6+ years", "5+ years",
]
REMOTE_SIGNALS = ["remote", "work from home", "wfh", "anywhere", "distributed",
                  "hybrid", "telework", "télétravail", "homeoffice"]

# How a posting describes where you sit. Checked against the title, location and
# the opening of the description, where employers actually state it.
WORK_MODE_SIGNALS = {
    "remote": ["fully remote", "100% remote", "remote-first", "work from home",
               "wfh", "remote position", "remote role", "telework", "distributed team"],
    "hybrid": ["hybrid", "flexible working", "2 days in", "3 days in",
               "days in the office", "part remote", "split between"],
    "onsite": ["on-site", "onsite", "in office", "in-office", "office-based",
               "on site", "5 days in the office", "fully in person"],
}


def work_mode_from_text(title: str, location: str, description: str, remote: bool) -> str:
    """Best guess at the working arrangement, or "" when the posting is silent.

    Takes plain strings rather than a Job so the store can call it straight off a
    database row (to correct a stale "remote" flag on read - see its docstring) without
    building a Job just to ask this one question. Text wins over the raw scraped flag:
    a widely reported complaint about Indeed and LinkedIn is that a job listed as
    "Remote" turns out, in its own description, to want you in an office two or three
    days a week - the flag says one thing, the small print says another, and this is
    what decides which one a user actually sees.
    """
    text = f"{title} {location} {description[:900]}".lower()
    hits = {mode: sum(1 for sig in signals if sig in text)
            for mode, signals in WORK_MODE_SIGNALS.items()}
    best = max(hits, key=lambda m: hits[m])
    if hits[best] == 0:
        return "remote" if remote else ""
    # "hybrid" wins over a bare "remote" mention, being the more specific claim.
    if hits["hybrid"] and hits["hybrid"] >= hits[best]:
        return "hybrid"
    return best


def detect_work_mode(job: Job) -> str:
    """Best guess at the working arrangement, or "" when the posting is silent."""
    return work_mode_from_text(job.title, job.location, job.description, job.remote)


def _age_days(job: Job) -> float | None:
    if not job.posted_at:
        return None
    try:
        posted = datetime.fromisoformat(job.posted_at)
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted).total_seconds() / 86400


EARLY_STAGES = ("student", "graduate")
# The classifier's own job kinds that are exactly what a student is after - it reads a
# board's label ("Placement/Internship" on Gradcracker) as well as the title, so this
# catches postings whose title alone would not say so.
STUDENT_KINDS = {"placement": "placement / year in industry", "internship": "internship",
                 "graduate_scheme": "graduate scheme", "apprenticeship": "apprenticeship"}
SENIOR_STAGES = ("professional", "senior")


def _stage(search) -> str:
    """The user's career stage. Explicit setting wins; otherwise infer from titles."""
    stage = (getattr(search, "career_stage", "") or "").strip().lower()
    if stage:
        return stage
    inferred = any(
        any(sig in t.lower() for sig in EARLY_CAREER)
        for t in (list(search.titles) + list(search.employment_types))
    )
    return "graduate" if inferred else "any"


def _wants_early_career(search) -> bool:
    return _stage(search) in EARLY_STAGES


def _markets(cfg) -> list:
    """The countries jobs are listed for - [None] when nothing is set, which means
    "no country preference" to _location_eligible."""
    markets = cfg.markets() if hasattr(cfg, "markets") else [cfg.home_country()]
    return markets or [None]


def _location_eligible(job: Job, search, home=None) -> bool:
    """True if someone based in the target market could actually take this job.

    Country-agnostic: `home` comes from the config, and geo decides whether a
    remote posting's stated scope includes that country or region.
    """
    text = f"{job.location} {job.title}".lower()
    targets = [loc.lower() for loc in search.locations]

    # No location criteria at all means everywhere is acceptable. Both callers
    # currently guard against this, but the function must be right on its own.
    if not targets and home is None:
        return True

    # Direct hit on a named target location.
    if any(re.search(rf"(?<![a-z]){re.escape(loc)}(?![a-z])", text) for loc in targets):
        return True

    # A place in our own country that we did not list explicitly. Resolve the whole
    # string rather than testing membership: "Lancaster, Pennsylvania" mentions a
    # UK town but is plainly a US job, and the more specific name settles it.
    if home:
        resolved = geo.countries_of(job.location, job.title)
        if home.code in resolved:
            return True
        if resolved:
            return False

    is_remote = job.remote or any(sig in text for sig in REMOTE_SIGNALS)
    if is_remote:
        return search.remote_ok and geo.remote_eligible(text, home)

    # The location names a place we cannot place. An employer's own board lists
    # bare towns, and no town list is complete, so unknown is not the same as
    # foreign - keep it and let the location score and the UI filter decide.
    if job.location and getattr(search, "unknown_location", "keep") == "keep":
        return True
    return False


@lru_cache(maxsize=8192)
def _registered_names(company: str) -> tuple:
    """Official/registered names for an employer, from company records."""
    from . import enrich
    try:
        profile = enrich.company_profile(company) or {}
    except Exception:
        return ()
    names = [profile.get("official_name"), profile.get("name")]

    # A company number identifies an employer exactly, where a name never can.
    # Resolve it to the registered name when a Companies House key is configured.
    number = profile.get("company_number")
    if number and (profile.get("jurisdiction") or "").lower().startswith("gb"):
        try:
            authoritative = sponsorship.registered_name_for_number(number)
        except Exception:
            authoritative = ""
        if authoritative:
            names.insert(0, authoritative)

    seen, out = set(), []
    for name in names:
        if name and name.lower() != company.lower() and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return tuple(out)


@lru_cache(maxsize=8192)
def _sponsor_lookup(company: str, country: str, city: str,
                    deep: bool = False) -> tuple | None:
    """Cached per company - a scrape sees the same employer many times over.

    `deep` escalates a weak name match by looking the employer's registered name up
    in company records. That is a network call per employer, so bulk paths leave it
    off and it runs on demand instead, when one job is actually being examined.
    """
    found = sponsorship.check(company, country, city)

    weak = (found is None
            or found.get("match") != "exact"
            or found.get("ambiguous"))
    if deep and weak:
        aliases = _registered_names(company)
        if aliases:
            better = sponsorship.check(company, country, city, aliases=list(aliases))
            if better and (found is None
                           or better.get("confidence", 0) > found.get("confidence", 0)
                           or better.get("match") == "registered name"):
                found = better

    if not found:
        return None
    return (found["name"], found["rating_letter"], found["confidence"],
            tuple(found["routes"]), found["match"], found["ambiguous"])


def sponsor_info(job: Job, cfg: Config, deep: bool = False) -> dict | None:
    """Sponsor-licence record for this job's employer, or None if not listed."""
    home = cfg.home_country()
    if home is None or not job.company:
        return None
    found = _sponsor_lookup(job.company, home.code, job.location or "", deep)
    if not found:
        return None
    name, rating, confidence, routes, match, ambiguous = found
    return {"name": name, "rating_letter": rating, "confidence": confidence,
            "routes": list(routes), "match": match, "ambiguous": ambiguous}


def hard_filter(job: Job, cfg: Config) -> str | None:
    """Return a rejection reason, or None if the job survives."""
    text = job.searchable()
    search = cfg.search

    # The user's own rules run first, and their reasons name the rule, so a posting that
    # vanishes because of a rule written last week says so on the Filtered tab.
    own = rules.hard_filter(cfg.rules(), job)
    if own:
        return own

    for bad in search.keywords_excluded:
        if bad.lower() in text:
            return f"excluded keyword: {bad}"

    for company in search.exclude_companies:
        if company.lower() in job.company.lower():
            return f"excluded company: {company}"

    for required in search.keywords_required:
        if required.lower() not in text:
            return f"missing required keyword: {required}"

    if search.max_age_days:
        age = _age_days(job)
        if age is not None and age > search.max_age_days:
            return f"older than {search.max_age_days}d ({age:.0f}d)"

    if search.locations or search.country:
        if not any(_location_eligible(job, search, m) for m in _markets(cfg)):
            return f"location mismatch: {job.location or 'unspecified'}"

    title = job.title.lower()
    stage = _stage(search)
    if stage in EARLY_STAGES and getattr(search, "exclude_senior", True):
        if any(sig in title for sig in SENIOR_SIGNALS):
            return f"senior role: {job.title}"
    if stage in SENIOR_STAGES and getattr(search, "exclude_junior", False):
        if any(sig in title for sig in EARLY_CAREER):
            return f"junior role: {job.title}"

    if search.salary_min and job.salary_max and job.salary_max < search.salary_min:
        return f"salary below {search.salary_min:,}"

    # What kind of work, and on what basis. Both are pure filters: an empty list
    # means no preference. A posting that never states its hours or field is kept
    # unless the user explicitly asks to hide unclassified ones.
    keep_unknown = getattr(search, "include_unclassified", True)
    for label, wanted_raw, actual, norm in (
        ("employment type", getattr(search, "employment_types", []),
         job.employment_kind, classify.normalise_kind),
        ("job family", getattr(search, "job_families", []),
         job.job_family, classify.normalise_family),
    ):
        wanted = {norm(w) for w in (wanted_raw or []) if w}
        if not wanted:
            continue
        if actual:
            if actual not in wanted:
                return f"{label}: {actual}"
        elif not keep_unknown:
            return f"{label} not stated"

    if getattr(search, "visa_sponsorship", "any") == "require":
        if sponsor_info(job, cfg) is None:
            return f"no sponsor licence found: {job.company}"

    return None


def _title_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    if not cfg.search.titles:
        return 0.6, []
    title = job.title.lower()
    best, best_target = 0.0, ""
    for target in cfg.search.titles:
        target = target.lower()
        if target in title:
            score = 1.0
        else:
            score = max(fuzz.partial_ratio(target, title),
                        fuzz.token_set_ratio(target, title)) / 100.0
        if score > best:
            best, best_target = score, target
    reasons = []
    if best >= 0.9:
        reasons.append(f"title matches '{best_target}'")
    elif best >= 0.7:
        reasons.append(f"title close to '{best_target}'")
    return best, reasons


def _level_adjust(job: Job, cfg: Config) -> tuple[float, list[str]]:
    """Push the ranking towards roles at the user's actual level, either direction."""
    stage = _stage(cfg.search)
    title = job.title.lower()
    head = f"{job.title} {job.description[:600]}".lower()

    if stage in EARLY_STAGES:
        if job.employment_kind in STUDENT_KINDS:
            return 8.0, [f"{STUDENT_KINDS[job.employment_kind]} - what you are looking for"]
        if any(sig in title for sig in EARLY_CAREER):
            return 8.0, ["early-career role (in title)"]
        if any(sig in head for sig in EARLY_CAREER):
            return 4.0, ["early-career wording in description"]
        if any(sig in title for sig in SENIOR_SIGNALS):
            return -18.0, ["looks senior - likely out of scope"]
        return -4.0, []

    if stage in SENIOR_STAGES:
        if any(sig in title for sig in EARLY_CAREER):
            return -16.0, ["junior/intern role - below your level"]
        if any(sig in title for sig in SENIOR_SIGNALS):
            return 7.0, ["seniority matches your level"]
        return 0.0, []

    return 0.0, []


def _keyword_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    wanted = [k.lower() for k in cfg.search.keywords_any]
    if not wanted:
        return 0.5, []
    text = job.searchable()
    hits = [k for k in wanted if k in text]
    if not hits:
        return 0.0, []
    score = min(1.0, len(hits) / max(3, len(wanted) * 0.4))
    return score, [f"keywords: {', '.join(hits[:6])}"]


def _cv_score(job: Job, profile: CVProfile | None) -> tuple[float, list[str], list[str], list[str]]:
    if not profile or not profile.skills:
        return 0.5, [], [], []
    job_skills = set(extract_skills(f"{job.title} {job.description}"))
    if not job_skills:
        return 0.35, [], [], []
    mine = set(profile.skills)
    matched = sorted(job_skills & mine)
    missing = sorted(job_skills - mine)
    coverage = len(matched) / len(job_skills)
    # Reward absolute overlap too - a job asking 10 skills you have 6 of beats
    # one asking 2 skills you have 2 of.
    depth = min(1.0, len(matched) / 6)
    score = 0.65 * coverage + 0.35 * depth
    reasons = []
    if matched:
        reasons.append(f"CV covers {len(matched)}/{len(job_skills)} listed skills: "
                       f"{', '.join(matched[:5])}")
    return score, reasons, matched, missing


def _location_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    if not cfg.search.locations:
        return 0.6, []
    text = f"{job.location} {job.title}".lower()
    for i, loc in enumerate(cfg.search.locations):
        if re.search(rf"(?<![a-z]){re.escape(loc.lower())}(?![a-z])", text):
            # earlier entries in the list are preferred locations
            return max(0.55, 1.0 - i * 0.12), [f"location: {job.location or loc}"]
    markets = _markets(cfg)
    resolved = geo.countries_of(job.location, job.title)
    if not resolved and job.location and not job.remote:
        return 0.35, [f"location not resolved: {job.location}"]
    if any(_location_eligible(job, cfg.search, m) for m in markets):
        listed = [m for m in markets if m and m.code in resolved]
        if listed:
            return 0.7, ["in " + " / ".join(m.name for m in listed)]
        return 0.7, ["remote - eligible from where you are"]
    return 0.25, []


def _recency_score(job: Job) -> tuple[float, list[str]]:
    age = _age_days(job)
    if age is None:
        return 0.45, []
    if age <= 3:
        return 1.0, [f"posted {age:.0f}d ago"]
    if age <= 14:
        return 0.85, [f"posted {age:.0f}d ago"]
    if age <= 30:
        return 0.6, []
    if age <= 60:
        return 0.35, []
    return 0.15, []


def _work_mode_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    """Rank towards the working arrangement the user asked for."""
    wanted = [m.lower() for m in getattr(cfg.search, "work_modes", []) or []]
    if not wanted:
        return 0.0, []
    mode = detect_work_mode(job)
    if not mode:
        return 0.0, []                    # silent posting: neither reward nor punish
    if mode in wanted:
        return 6.0, [f"{mode} working, which is what you asked for"]
    return -7.0, [f"{mode} working, and you asked for {' or '.join(wanted)}"]


def _sponsorship_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    """Reward employers who can actually hire someone needing a visa."""
    mode = getattr(cfg.search, "visa_sponsorship", "any")
    if mode == "any":
        return 0.0, []
    found = sponsor_info(job, cfg)
    if not found:
        return 0.0, ["no sponsor licence on the official register"]

    score = 0.75 if found["rating_letter"] == "A" else 0.45
    if found["confidence"] < 0.6 or found["ambiguous"]:
        score *= 0.7            # name match is uncertain, so weight it less
    if any("graduate" in r.lower() for r in found["routes"]):
        score = min(1.0, score + 0.25)
    note = (f"licensed sponsor ({found['rating_letter']} rating) "
            f"as \"{found['name']}\"")
    if found["ambiguous"]:
        note += " - name match is ambiguous, check it"
    return score, [note]


def _salary_score(job: Job, cfg: Config) -> tuple[float, list[str]]:
    if not (job.salary_min or job.salary_max):
        return 0.4, []
    top = job.salary_max or job.salary_min or 0
    floor = cfg.search.salary_min or 0
    if not floor:
        return 0.7, ([f"salary listed: {job.salary_display}"] if job.salary_display else [])
    ratio = top / floor if floor else 1.0
    return min(1.0, max(0.0, (ratio - 0.8) / 0.7)), [f"salary {job.salary_display}"]


def score_job(job: Job, cfg: Config, profile: CVProfile | None = None) -> Job:
    """Attach score, reasons and skill gap to a job. Returns the same object."""
    weights = cfg.weights
    reasons: list[str] = []

    title, why = _title_score(job, cfg);      reasons += why
    keywords, why = _keyword_score(job, cfg); reasons += why
    cvs, why, matched, missing = _cv_score(job, profile); reasons += why
    location, why = _location_score(job, cfg); reasons += why
    recency, why = _recency_score(job);        reasons += why
    salary, why = _salary_score(job, cfg);     reasons += why
    sponsor, why = _sponsorship_score(job, cfg); reasons += why
    mode, why = _work_mode_score(job, cfg);    reasons += why
    level, why = _level_adjust(job, cfg);      reasons += why
    own, why = rules.score(cfg.rules(), job);  reasons += why

    total = (title * weights.title + keywords * weights.keywords + cvs * weights.cv_skills
             + location * weights.location + recency * weights.recency
             + salary * weights.salary + level + mode + own
             + sponsor * getattr(weights, "sponsorship", 0.0))

    if job.source_kind == "ats_direct":
        total += 3.0
        reasons.append("direct from company board")

    job.score = round(max(0.0, min(100.0, total)), 1)
    job.score_reasons = reasons
    job.matched_skills = matched
    job.missing_skills = missing[:12]
    return job


def rank(jobs: list[Job], cfg: Config, profile: CVProfile | None = None
         ) -> tuple[list[Job], dict[str, int]]:
    """Filter, score and sort. Also returns a tally of why things were dropped."""
    kept: list[Job] = []
    dropped: dict[str, int] = {}
    for job in jobs:
        reason = hard_filter(job, cfg)
        if reason:
            # Collapse "older than 60d (85d)" / "excluded keyword: foo" to one bucket each.
            key = re.sub(r"\s*[:(].*", "", reason).strip()
            dropped[key] = dropped.get(key, 0) + 1
            continue
        score_job(job, cfg, profile)
        if job.score >= cfg.min_score:
            kept.append(job)
        else:
            dropped["below min_score"] = dropped.get("below min_score", 0) + 1
    kept.sort(key=lambda j: j.score, reverse=True)
    return kept, dropped
