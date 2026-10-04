"""Run a full scrape: gather -> filter -> score -> persist."""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import re
from pathlib import Path
from typing import Callable

import yaml

from . import harvest, sponsorship, stability
from .http import SESSION
from .config import Config
from .cv import CVProfile, build_profile
from .models import Job
from .scoring import hard_filter, rank, score_job, sponsor_info
from .sources import ALL_ADAPTERS, ATS_ADAPTERS
from .sources import ats
from .sources.ats import set_home_country
from .store import Store, now

log = logging.getLogger("jobscout.pipeline")
ROOT = Path(__file__).resolve().parent.parent

Progress = Callable[[str, dict], None]


def _noop(_stage: str, _info: dict) -> None:
    pass


def _warm_sponsor_register(cfg: Config, progress: Progress = _noop) -> None:
    """Load the sponsor register up front, not lazily in the middle of a scrape."""
    if getattr(cfg.search, "visa_sponsorship", "any") == "any":
        return
    home = cfg.home_country()
    register = sponsorship.register_for(home.code if home else None)
    if register is None:
        log.info("no sponsor register published for %s",
                 home.name if home else "this market")
        return
    progress("start", {"message": "loading visa sponsor register"})
    register.load()


def _annotate_sponsors(cfg: Config, store: Store, progress: Progress = _noop) -> int:
    """Record each employer's sponsor-licence status against its rows.

    Reads the rows back out of the store rather than using the scraped Job objects:
    upsert collapses cross-source duplicates onto the first row's id, so annotating
    by the scraped id silently missed every deduped posting.
    """
    if getattr(cfg.search, "visa_sponsorship", "any") == "any":
        return 0
    progress("start", {"message": "checking sponsor licences"})
    tagged = 0
    for row in store.query(limit=200000, min_score=0.0, include_filtered=True):
        probe = Job(source=row["source"], source_kind=row["source_kind"],
                    company=row["company"], title=row["title"], url=row["url"],
                    location=row["location"] or "")
        info = sponsor_info(probe, cfg)
        store.set_sponsor(row["id"], info)
        tagged += bool(info)
    store.conn.commit()
    return tagged


def load_companies(cfg: Config, harvested: bool = True) -> list[dict]:
    path = ROOT / cfg.sources.companies_file
    if not path.exists():
        log.warning("no company registry at %s - run tools/discover_slugs.py", path)
        return []
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    companies = list(data.get("companies", []))
    # Extra employers from tools/discover_startups.py (graduate employers and hiring
    # startups), kept in their own file so re-running discovery never rewrites the
    # hand-verified registry. Same schema; an employer already present wins.
    extra_path = path.with_name("companies_extra.yaml")
    if extra_path.exists():
        try:
            extra = yaml.safe_load(extra_path.read_text("utf-8")) or {}
        except yaml.YAMLError as exc:
            log.warning("could not read %s: %s", extra_path, exc)
            extra = {}
        seen = {(c.get("ats"), str(c.get("slug", "")).lower()) for c in companies}
        for entry in extra.get("companies", []) or []:
            if not isinstance(entry, dict) or not entry.get("company"):
                continue
            key = (entry.get("ats"), str(entry.get("slug", "")).lower())
            if key not in seen:
                companies.append(entry)
                seen.add(key)
    # Boards found behind job-board apply links (jobscout/harvest.py), last so a
    # hand-kept entry for the same board always wins.
    if harvested and getattr(cfg.sources, "harvest_boards", True):
        seen = {(c.get("ats"), str(c.get("slug", "")).lower()) for c in companies}
        for entry in harvest.load_harvested():
            key = (entry.get("ats"), str(entry.get("slug", "")).lower())
            if entry.get("company") and entry.get("ats") in ATS_ADAPTERS and key not in seen:
                companies.append(entry)
                seen.add(key)
    if cfg.sources.company_limit:
        companies = companies[: cfg.sources.company_limit]
    return companies


def load_profile(cfg: Config, derived: dict | None = None) -> CVProfile | None:
    """The profile used for matching, LinkedIn first.

    ``derived`` is the stored profile from the signed-in account, built by
    linkedin_import from the user's own export. It wins over a CV because it is
    structured: LinkedIn separates skills, degree and dates, where a CV parser has to
    guess. A CV is read only when there is no imported profile, and extra_skills the
    user typed in are added to whichever won.
    """
    profile = None
    if derived:
        profile = CVProfile.from_dict(derived)
    elif cfg.profile.cv_path:
        try:
            profile = build_profile(cfg.profile.cv_path)
        except Exception as exc:
            log.error("could not read CV %s: %s", cfg.profile.cv_path, exc)
            return None
    if profile is None:
        return None
    for extra in (cfg.profile.extra_skills or []):
        skill = str(extra).strip().lower()
        if skill and skill not in profile.skills:
            profile.skills.append(skill)
            profile.custom_skills.append(skill)
            profile.skills_by_group.setdefault("yours", []).append(skill)
    return profile


# Kept so older callers and scripts do not break.
def load_cv(cfg: Config) -> CVProfile | None:
    return load_profile(cfg)


# Postings returned per (ats, slug) board in the most recent gather_ats() call.
LAST_BOARD_COUNTS: dict[tuple[str, str], int] = {}

# Adapters that page through a board and record whether they reached its real end,
# so an empty board from one of them is known to be genuinely empty. For the others a
# single failed request also comes back empty, so empty proves nothing.
_PAGING_ADAPTERS = ("workday", "smartrecruiters")


def fully_read_companies(companies: list[dict]) -> dict[str, list[str]]:
    """Company -> its adapters, for every employer ALL of whose boards were read in full.

    Removal is judged per company (rows do not record which of an employer's boards
    they came from), so one board cut off at the cap, or failed part-way, rules the
    whole employer out - AstraZeneca's complete 23-posting graduate board must not
    vouch for its capped 1,033-posting main board.
    """
    boards: dict[str, list[tuple[str, str]]] = {}
    for entry in companies:
        boards.setdefault(entry.get("company"), []).append(
            (entry.get("ats", ""), entry.get("slug", "")))
    complete: dict[str, list[str]] = {}
    for company, its_boards in boards.items():
        counts = [LAST_BOARD_COUNTS.get(b) for b in its_boards]
        if any(n is None for n in counts) or sum(counts) == 0:
            continue
        if all(n < ats.MAX_PER_COMPANY and ats.fetch_was_complete(*b)
               and (n > 0 or b[0] in _PAGING_ADAPTERS)
               for b, n in zip(its_boards, counts)):
            complete[company] = sorted({b[0] for b in its_boards})   # rows' source
    return complete


def gather_ats(cfg: Config, progress: Progress = _noop) -> tuple[list[Job], dict]:
    set_home_country(cfg.home_country())      # so salary parsing knows the market
    ats.MAX_PER_COMPANY = int(getattr(cfg.sources, "max_per_company", 400) or 400)
    companies = load_companies(cfg)
    if not companies or not cfg.sources.use_ats:
        return [], {}
    jobs: list[Job] = []
    per_company: dict[str, int] = {}

    LAST_BOARD_COUNTS.clear()

    def one(entry: dict) -> tuple[dict, list[Job]]:
        adapter = ATS_ADAPTERS.get(entry.get("ats", ""))
        if not adapter:
            return entry, []
        try:
            return entry, adapter.fetch(entry["slug"], entry["company"])
        except Exception as exc:
            log.debug("%s (%s) failed: %s", entry.get("company"), entry.get("ats"), exc)
            ats._record_completeness(entry.get("ats", ""), entry.get("slug", ""), False)
            return entry, []

    # The throttle is per host, so workers on different employers' boards no longer
    # queue behind one another; 16 keeps every host at its 1 request/second.
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(one, c) for c in companies]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            entry, found = fut.result()
            company = entry.get("company", "?")
            jobs.extend(found)
            # Summed, not overwritten: several employers have two boards (a graduate
            # site and a main one), and the second used to replace the first's count.
            per_company[company] = per_company.get(company, 0) + len(found)
            LAST_BOARD_COUNTS[(entry.get("ats", ""), entry.get("slug", ""))] = len(found)
            progress("ats", {"done": i, "total": len(companies),
                             "company": company, "found": len(found),
                             "running_total": len(jobs)})
    return jobs, per_company


def gather_aggregators(cfg: Config, progress: Progress = _noop) -> tuple[list[Job], dict]:
    jobs: list[Job] = []
    per_source: dict[str, int] = {}
    queries = cfg.sources.aggregator_queries or [""]
    wanted = [n for n in cfg.sources.aggregators if n in ALL_ADAPTERS]
    home = cfg.home_country()

    for name in wanted:
        adapter = ALL_ADAPTERS[name]
        if not adapter.serves(home):
            per_source[name] = -2          # -2 == does not cover this market
            progress("aggregator", {"source": name, "found": 0,
                                    "skipped": f"no coverage in {home.name}"})
            continue
        if getattr(adapter, "needs_key", False) and not adapter.available():
            per_source[name] = -1          # -1 == configured but no API key
            progress("aggregator", {"source": name, "found": 0, "skipped": "no API key"})
            continue
        # A couple of the community adapters take a source-specific kwarg beyond the
        # common query/location/max_results/home signature every BaseAggregator shares.
        extra: dict = {}
        if name == "reddit":
            extra["subreddits"] = tuple(cfg.sources.reddit_subreddits or ())
        elif name == "custom_feeds":
            extra["feed_urls"] = list(cfg.sources.custom_feed_urls or [])

        # A source that ignores the query returns the same listing every time, so it is
        # asked once with the whole budget rather than once per query - which fetched
        # the same first page N times and kept only max_per_source of, say, the GitHub
        # trackers' 2,000+ postings.
        uses_query = getattr(adapter, "uses_query", True)
        runs = queries if uses_query else [""]
        budget = cfg.sources.max_per_source * (1 if uses_query else len(queries))
        override = (cfg.sources.source_limits or {}).get(name)
        if override:
            budget = int(override)

        found_here: list[Job] = []
        for query in runs:
            try:
                found_here += adapter.fetch(
                    query=query,
                    location=(cfg.search.locations[0] if cfg.search.locations else ""),
                    max_results=budget,
                    home=home,
                    **extra,
                )
            except Exception as exc:
                log.debug("aggregator %s failed on %r: %s", name, query, exc)
        jobs.extend(found_here)
        per_source[name] = len(found_here)
        progress("aggregator", {"source": name, "found": len(found_here),
                                "running_total": len(jobs)})
    return jobs, per_source


def _harvest_boards(cfg: Config, agg_jobs: list[Job]) -> dict:
    """Save the ATS boards behind home-market boards' apply links for the next scan.

    Only boards that serve the user's own market are mined: the GitHub trackers link
    to hundreds of US-only employers, which would slow every scan for nothing.
    """
    if not getattr(cfg.sources, "harvest_boards", True) or not cfg.sources.use_ats:
        return {"added": 0, "dropped": 0, "total": 0}
    home = cfg.home_country()
    local = [j for j in agg_jobs
             if home and home.code in getattr(ALL_ADAPTERS.get(j.source), "markets", ())]
    known = {(c.get("ats"), str(c.get("slug", "")).lower())
             for c in load_companies(cfg, harvested=False)}
    try:
        return harvest.update(local, known, dict(LAST_BOARD_COUNTS))
    except Exception as exc:              # growing the list must never fail a scan
        log.warning("board harvest failed: %s", exc)
        return {"added": 0, "dropped": 0, "total": 0}


def source_health(per_source: dict, per_company: dict, previous: dict | None) -> dict:
    """What broke this scan, judged against the last one.

    A source that returned postings last time and none now has almost always changed
    its page or API rather than run out of jobs, and fails silently otherwise - the
    scan just looks a bit smaller. Same for an employer board that went from many
    postings to none. Also carries the HTTP layer's failing hosts and counters.
    """
    previous = previous or {}
    before_src = previous.get("per_source") or {}
    before_co = previous.get("per_company") or {}
    silent = sorted(name for name, n in per_source.items()
                    if n == 0 and (before_src.get(name) or 0) >= 5)
    boards = sorted(name for name, n in before_co.items()
                    if n >= 5 and not per_company.get(name))
    http = SESSION.health()
    return {
        "sources_gone_quiet": [{"source": n, "was": before_src[n]} for n in silent],
        "boards_gone_quiet": [{"company": n, "was": before_co[n]} for n in boards][:25],
        "failing_hosts": http["failing_hosts"],
        "http": http["stats"],
    }


def refresh_registry_if_stale(cfg: Config, progress: Progress = _noop,
                              runner=None, path: Path | None = None) -> str:
    """Rebuild data/companies_extra.yaml when it is older than registry_refresh_days.

    Run after a scan, not before it: the ~2,500 probe requests take minutes, and
    nobody should wait that long for results. The refreshed list is used from the
    next scan. ``runner`` exists so the audit can exercise this without the network.
    """
    days = int(getattr(cfg.sources, "registry_refresh_days", 0) or 0)
    path = path or ROOT / "data" / "companies_extra.yaml"
    if days <= 0:
        return "disabled"
    import time as _time
    if path.exists() and (_time.time() - path.stat().st_mtime) < days * 86400:
        return "fresh"
    progress("start", {"message": "refreshing the company list (every "
                                  f"{days} days - takes a few minutes)"})
    import subprocess
    import sys
    run_it = runner or (lambda: subprocess.run(
        [sys.executable, str(ROOT / "tools" / "discover_startups.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=1200))
    try:
        result = run_it()
    except Exception as exc:
        log.warning("company list refresh failed: %s", exc)
        return "failed"
    if getattr(result, "returncode", 0) != 0:
        log.warning("company list refresh exited %s: %s", result.returncode,
                    (getattr(result, "stderr", "") or "")[-400:])
        return "failed"
    stability.reset_tags()            # new startup tags apply from now on
    return "refreshed"


def run(cfg: Config | None = None, store: Store | None = None,
        progress: Progress = _noop, derived_profile: dict | None = None) -> dict:
    cfg = cfg or Config.load()
    store = store or Store()
    started = now()
    previous = store.last_run_detail()
    SESSION.reset_health()

    progress("start", {"message": "reading your profile"})
    profile = load_profile(cfg, derived_profile)
    _warm_sponsor_register(cfg, progress)

    progress("start", {"message": "querying company boards"})
    ats_jobs, per_company = gather_ats(cfg, progress)

    progress("start", {"message": "querying job boards"})
    agg_jobs, per_source = gather_aggregators(cfg, progress)

    harvested = _harvest_boards(cfg, agg_jobs)

    raw = ats_jobs + agg_jobs
    progress("scoring", {"total": len(raw)})
    kept, dropped = rank(raw, cfg, profile)

    new_count, updated = store.upsert(kept)
    store.apply_startup_flags(stability.startup_flags())
    sponsored = _annotate_sponsors(cfg, store, progress)

    # Every scan refreshes everything stored, not only what this scan returned:
    # postings an employer took down (boards read in full only - one cut off at the
    # per-company cap proves nothing) or whose closing date passed are flagged, and
    # every row is rescored against today's criteria, profile and scoring rules.
    delisted = store.mark_delisted(started, fully_read_companies(load_companies(cfg)),
                                   # by key AND id: an edited title changes the key
                                   fetched_keys={j.dedupe_key for j in raw}
                                   | {j.id for j in raw})
    progress("start", {"message": "rescoring everything stored"})
    refreshed = rescore(cfg, store, derived_profile, prune=True)
    registry = refresh_registry_if_stale(cfg, progress)
    detail = {
        "per_company": {k: v for k, v in sorted(
            per_company.items(), key=lambda x: -x[1]) if v},
        "per_source": per_source,
        "dropped": dropped,
        "ats_raw": len(ats_jobs),
        "aggregator_raw": len(agg_jobs),
        "profile": profile.to_dict() if profile else None,
        "health": source_health(per_source, per_company, previous),
    }
    store.record_run(started, len(raw), len(kept), new_count, detail)
    SESSION.prune_cache()          # keep the on-disk HTTP cache from growing forever

    summary = {
        "found": len(raw), "kept": len(kept), "new": new_count, "updated": updated,
        "dropped": dropped, "per_source": per_source,
        "companies_queried": len(per_company),
        "companies_with_jobs": sum(1 for v in per_company.values() if v),
        "sponsored": sponsored,
        "delisted": delisted,
        "rescored": refreshed.get("kept", 0),
        "registry_refresh": registry,
        "health": detail["health"],
        "boards_harvested": harvested,
        "profile_skills": profile.skills if profile else [],
    }
    progress("done", summary)
    return summary


def rescore(cfg: Config | None = None, store: Store | None = None,
            derived_profile: dict | None = None,
            prune: bool = True) -> dict:
    """Re-apply filters and scoring to rows already in the database.

    Network-free. Use after changing criteria so the stored set reflects the new rules
    without waiting for a full scrape. Rows that now fail a hard filter are deleted
    unless the user has engaged with them (starred, or moved off 'new').
    """
    cfg = cfg or Config.load()
    store = store or Store()
    profile = load_profile(cfg, derived_profile)
    set_home_country(cfg.home_country())
    _warm_sponsor_register(cfg)

    # Include already-hidden rows: a criteria change can make them valid again.
    rows = store.query(limit=100000, min_score=0.0, include_filtered=True)
    kept = dropped = protected = restored = 0
    drop_reasons: dict[str, int] = {}

    for row in rows:
        job = Job(
            source=row["source"], source_kind=row["source_kind"], company=row["company"],
            title=row["title"], url=row["url"], location=row["location"] or "",
            remote=bool(row["remote"]), description=row["description"] or "",
            department=row["department"] or "", employment_type=row["employment_type"] or "",
            salary_min=row["salary_min"], salary_max=row["salary_max"],
            salary_currency=row["salary_currency"] or "", posted_at=row["posted_at"],
            closes_at=row["closes_at"],
        )
        reason = hard_filter(job, cfg)
        engaged = row["starred"] or row["status"] not in ("new",)

        if reason and prune and not engaged:
            # Hidden, not deleted - change the criteria back and it returns.
            store.set_filtered(row["id"], True, reason)
            key = re.sub(r"\s*[:(].*", "", reason).strip()
            drop_reasons[key] = drop_reasons.get(key, 0) + 1
            dropped += 1
            continue
        if reason and engaged:
            protected += 1
        elif row.get("filtered"):
            store.set_filtered(row["id"], False)
            restored += 1

        score_job(job, cfg, profile)
        if getattr(cfg.search, "visa_sponsorship", "any") != "any":
            store.set_sponsor(row["id"], sponsor_info(job, cfg))  # row id, not job id
        # Rebuilding the Job re-runs classification, so a fix to the classifier
        # reaches stored rows on the next rescore rather than only on new ones.
        store.conn.execute(
            "UPDATE jobs SET score = ?, score_reasons = ?, matched_skills = ?, "
            "missing_skills = ?, employment_kind = ?, job_family = ? WHERE id = ?",
            (job.score, json.dumps(job.score_reasons), json.dumps(job.matched_skills),
             json.dumps(job.missing_skills), job.employment_kind, job.job_family,
             row["id"]),
        )
        kept += 1

    store.conn.commit()
    store.apply_startup_flags(stability.startup_flags())
    return {"kept": kept, "dropped": dropped, "protected": protected,
            "restored": restored, "drop_reasons": drop_reasons}
