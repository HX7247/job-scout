"""Grow the company list from the apply links job boards hand us.

A TARGETjobs posting for a Lloyds placement links to lbg.wd3.myworkdayjobs.com; that
board holds every Lloyds posting, not just the one advertised. So after each scan the
apply links from the home-market boards are mapped to the ATS boards behind them and
new ones are saved to data/companies_harvested.yaml, read from the next scan on.

Self-pruning: a harvested board that comes back empty or unreadable on MISS_LIMIT
scans in a row is dropped, so a one-off link or a board the employer retired does
not cost requests forever. The hand-verified registry is never touched.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger("jobscout.harvest")

ROOT = Path(__file__).resolve().parent.parent
HARVEST_FILE = ROOT / "data" / "companies_harvested.yaml"
MISS_LIMIT = 3
MAX_BOARDS = 300          # a ceiling on what one scan can grow the list to

_PATTERNS: list[tuple[str, re.Pattern, callable]] = [
    ("workday", re.compile(
        r"^https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?"
        r"([\w-]+)(?:/|$)"),
     lambda m: f"{m[1]}:{m[2]}:{m[3]}"),
    ("workday", re.compile(
        r"^https?://(wd\d+)\.myworkdaysite\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?recruiting/"
        r"([\w-]+)/([\w-]+)(?:/|$)"),
     lambda m: f"{m[2]}:{m[1]}:{m[3]}:myworkdaysite"),
    ("oracle", re.compile(
        r"^https?://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/"
        r"sites/([\w-]+)"),
     lambda m: f"{m[1]}/{m[2]}"),
    ("greenhouse", re.compile(
        r"^https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_(?:app|board)\?for=)?([\w-]+)"),
     lambda m: m[1]),
    ("lever", re.compile(r"^https?://jobs\.lever\.co/([\w.-]+)(?:[/?#]|$)"), lambda m: m[1]),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([\w.%-]+)(?:[/?#]|$)"), lambda m: m[1]),
    # No SmartRecruiters: api.smartrecruiters.com's robots.txt disallows everyone but
    # LinkedIn, so a harvested board there could never be read.
    ("workable", re.compile(r"^https?://apply\.workable\.com/([\w-]+)(?:[/?#]|$)"), lambda m: m[1]),
]
# Path segments that look like a board but are the ATS's own plumbing, and a bare
# locale (".../en-GB" with no board after it), which Workday puts where a board goes.
_NOT_A_SITE = {"wday", "job", "jobs", "login", "recruiting", "embed", "api", "v1"}
_LOCALE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.I)


# TARGETjobs names employers it scraped itself after their board URL ("Organon
# Searchjobs", "Mmc Careers"); the trailing board word is not part of the name.
_BOARD_WORDS = {"careers", "career", "searchjobs", "jobs", "external", "externalcareers",
                "externalcareersite", "careersite", "jobsearch", "recruiting"}


def clean_company(name: str) -> str:
    words = (name or "").split()
    while len(words) > 1 and words[-1].lower() in _BOARD_WORDS:
        words.pop()
    return " ".join(words)


def board_for(url: str) -> tuple[str, str] | None:
    """(ats, slug) for an apply link on a supported ATS, else None."""
    url = (url or "").strip()
    if not urlparse(url).netloc:
        return None
    for ats, pattern, slug_of in _PATTERNS:
        m = pattern.match(url)
        if m:
            slug = slug_of(m)
            board = slug.split(":")[2] if ats == "workday" else slug
            if board.lower() in _NOT_A_SITE or _LOCALE.match(board):
                return None
            return ats, slug
    return None


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text("utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    entries = [e for e in data.get("companies", []) or [] if isinstance(e, dict)]
    for entry in entries:
        entry["company"] = clean_company(str(entry.get("company") or ""))
    return entries


def _write(path: Path, entries: list[dict]) -> None:
    header = ("# Written by jobscout/harvest.py after each scan - boards found behind job\n"
              "# board apply links. Safe to delete; it regrows. Edit the verified list instead.\n")
    path.write_text(header + yaml.safe_dump({"companies": entries}, sort_keys=False,
                                            allow_unicode=True), "utf-8")


def load_harvested(path: Path | None = None) -> list[dict]:
    return _read(path or HARVEST_FILE)


def update(jobs, known: set[tuple[str, str]], board_counts: dict,
           path: Path | None = None) -> dict:
    """Add boards found in ``jobs``' links; age out harvested boards that stay empty.

    ``known`` is every (ats, lower-case slug) already in the hand-kept registries;
    ``board_counts`` maps (ats, slug) -> postings this scan read from each board.
    """
    path = path or HARVEST_FILE
    entries = _read(path)
    have = {(e.get("ats"), str(e.get("slug", "")).lower()) for e in entries}

    kept, dropped = [], 0
    for entry in entries:
        key = (entry.get("ats"), entry.get("slug"))
        if key in board_counts:                  # read this scan
            count = board_counts[key]
            if count:
                entry["jobs_seen"] = count
                entry["misses"] = 0
            else:
                entry["misses"] = int(entry.get("misses", 0)) + 1
        if int(entry.get("misses", 0)) >= MISS_LIMIT:
            dropped += 1
            continue
        kept.append(entry)

    added = 0
    for job in jobs:
        for link in (getattr(job, "raw", {}) or {}).get("apply_url"), job.url:
            found = board_for(link or "")
            if not found or not job.company:
                continue
            ats, slug = found
            key = (ats, slug.lower())
            if key in known or key in have or len(kept) >= MAX_BOARDS:
                continue
            kept.append({"company": clean_company(job.company), "ats": ats, "slug": slug,
                         "found_via": job.source, "misses": 0})
            have.add(key)
            added += 1
    if added or dropped or any(e.get("misses") for e in kept) or entries != kept:
        try:
            _write(path, kept)
        except OSError as exc:
            log.warning("could not save harvested boards: %s", exc)
    return {"added": added, "dropped": dropped, "total": len(kept)}
