"""Company stability - an honest stand-in for "job security" from public records.

What people usually want from Glassdoor here is a sense of whether the employer is
solid. Glassdoor cannot supply that legitimately (see enrich.py's docstring: no
public API since 2022, scraping breaches its terms), and employee star ratings are
not a stability measure anyway. So this rates the EMPLOYER from records that can be
checked, and shows every fact it used with where it came from:

  Companies House  UK company register (free key, COMPANIES_HOUSE_KEY): whether the
                   company is active or in liquidation/administration, insolvency
                   history, overdue accounts, incorporation date.
  Wikidata         founding year, headcount - only counted when the match is
                   confirmed (apply URL on the company's own domain, or an exact name
                   match), because name search happily returns a namesake.
  Sponsor register the Home Office licence rating: A is in good standing, B means a
                   compliance downgrade.
  Company registry startup tags from tools/discover_startups.py (e.g. YC batch).
  This app's data  how many roles the employer has open right now, and how many of
                   them have sat unfilled for 45+ days.

It never produces a level from one weak signal: with fewer than two usable facts the
answer is "not enough data", said plainly.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .http import SESSION

log = logging.getLogger("jobscout.stability")

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "stability_cache.json"
COMPANIES_HOUSE_API = "https://api.company-information.service.gov.uk"

DISCLAIMER = ("Built from public company records, not employee reviews - Glassdoor and "
              "similar review sites do not allow programmatic access. It says whether "
              "the employer looks established and in good standing, not what working "
              "there is like.")

LEVELS = {
    "caution": "Check before applying",
    "established": "Established",
    "stable": "Stable",
    "early": "Early-stage",
    "unknown": "Not enough data",
}

# Companies House statuses that mean the company is winding up or gone.
_BAD_STATUSES = {"dissolved", "liquidation", "administration", "receivership",
                 "converted-closed", "insolvency-proceedings", "voluntary-arrangement",
                 "removed", "closed"}


def _domain_root(url_or_host: str) -> str:
    host = urlparse(url_or_host).netloc if "//" in (url_or_host or "") else (url_or_host or "")
    parts = host.lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def wikidata_confirmed(company: str, profile: dict | None, job_url: str = "") -> str:
    """Why we trust the Wikidata match, or "" if we do not."""
    if not profile or profile.get("source") != "wikidata":
        return ""
    if profile.get("verified_by"):
        return profile["verified_by"]
    root = _domain_root(profile.get("domain", ""))
    if root and job_url and root in (urlparse(job_url).netloc or "").lower():
        return f"apply URL is on {profile.get('domain')}"
    if _norm(profile.get("name", "")) == _norm(company):
        return "exact name match"
    return ""


# ------------------------------------------------------------------ registry tags
_TAGS: dict | None = None


def registry_tags(company: str) -> dict:
    """Startup/founded/team-size tags written by the company-discovery tools."""
    global _TAGS
    if _TAGS is None:
        _TAGS = {}
        for name in ("companies_verified.yaml", "companies_extra.yaml"):
            path = ROOT / "data" / name
            try:
                data = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
            except (OSError, yaml.YAMLError):
                data = {}
            for entry in (data or {}).get("companies") or []:
                if isinstance(entry, dict) and entry.get("company"):
                    _TAGS.setdefault(entry["company"].strip().lower(), {}).update(
                        {k: v for k, v in entry.items()
                         if k in ("startup", "startup_evidence", "founded", "team_size",
                                  "source")})
        path = ROOT / "data" / "company_tags.yaml"
        try:
            extra = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
        except (OSError, yaml.YAMLError):
            extra = {}
        # tools/discover_startups.py writes {"meta": ..., "tags": {company: {...}}}.
        extra = (extra or {}).get("tags") or {}
        for name, tags in extra.items():
            if isinstance(tags, dict):
                _TAGS.setdefault(str(name).strip().lower(), {}).update(tags)
    return dict(_TAGS.get((company or "").strip().lower(), {}))


def reset_tags() -> None:
    global _TAGS
    _TAGS = None


def startup_flags() -> dict[str, bool]:
    """Every registry company with an explicit startup true/false, keyed lower-case."""
    registry_tags("")                       # load once
    return {name: bool(tags["startup"]) for name, tags in (_TAGS or {}).items()
            if isinstance(tags.get("startup"), bool)}


# ------------------------------------------------------------------ Companies House
def companies_house(company_number: str) -> dict | None:
    """The register's own record for one UK company, or None (no key, not found)."""
    key = os.environ.get("COMPANIES_HOUSE_KEY")
    number = (company_number or "").strip().upper()
    if not key or not re.fullmatch(r"[A-Z0-9]{6,8}", number):
        return None
    token = base64.b64encode(f"{key}:".encode()).decode()
    return SESSION.get_json(f"{COMPANIES_HOUSE_API}/company/{number}",
                            headers={"Authorization": f"Basic {token}"},
                            check_robots=False)


# ------------------------------------------------------------------ the rating
SPONSOR_MIN_CONFIDENCE = 0.7      # the same bar the detail panel's caveat uses


def assess(company: str, *, profile: dict | None = None, sponsor_rating: str = "",
           sponsor_name: str = "", sponsor_confidence: float = 1.0,
           job_url: str = "", activity: dict | None = None,
           tags: dict | None = None, register: dict | None = None,
           today: date | None = None) -> dict:
    """Rate one employer. Every input is optional; missing ones are reported, not guessed.

    ``register`` is a Companies House company record; when omitted it is fetched if the
    Wikidata match is confirmed, is a UK company and a key is configured.
    """
    today = today or date.today()
    tags = registry_tags(company) if tags is None else tags
    factors: list[dict] = []
    not_checked: list[str] = []
    points = 0
    caution = False
    facts = 0

    def add(label: str, detail: str, effect: int, source: str, url: str = "") -> None:
        factors.append({"label": label, "detail": detail,
                        "effect": "+" if effect > 0 else "-" if effect < 0 else "=",
                        "source": source, "url": url})

    confirmed = wikidata_confirmed(company, profile, job_url)

    # --- Companies House
    if register is None and confirmed and (profile or {}).get("jurisdiction") == "gb":
        try:
            register = companies_house(profile.get("company_number", ""))
        except Exception as exc:          # never let a lookup break the panel
            log.debug("companies house lookup failed for %s: %s", company, exc)
            register = None
    if register:
        facts += 1
        number = register.get("company_number", "")
        url = f"https://find-and-update.company-information.service.gov.uk/company/{number}"
        status = (register.get("company_status") or "").lower()
        if status in _BAD_STATUSES:
            caution = True
            add("Company status", f"Registered as {status.replace('-', ' ')}", -2,
                "Companies House", url)
        elif status == "active":
            add("Company status", "Active on the UK register", 1, "Companies House", url)
            points += 1
        if register.get("has_insolvency_history"):
            caution = True
            add("Insolvency history", "The register records past insolvency proceedings",
                -2, "Companies House", url)
        if (register.get("accounts") or {}).get("overdue"):
            points -= 1
            add("Accounts overdue", "Annual accounts are late at Companies House", -1,
                "Companies House", url)
        created = register.get("date_of_creation") or ""
        if re.match(r"\d{4}", created):
            age = today.year - int(created[:4])
            points += 2 if age >= 20 else 1 if age >= 8 else -1 if age < 3 else 0
            add("Incorporated", f"{created[:4]} ({age} years ago)",
                1 if age >= 8 else -1 if age < 3 else 0, "Companies House", url)
    elif os.environ.get("COMPANIES_HOUSE_KEY"):
        not_checked.append("Companies House: no confirmed UK company number for this name")
    else:
        not_checked.append("Companies House: add a free COMPANIES_HOUSE_KEY to check "
                           "active status, insolvency and overdue accounts")

    # --- Wikidata, only when the match is confirmed
    if profile and profile.get("source") == "wikidata":
        wd_url = (f"https://www.wikidata.org/wiki/{profile['wikidata_id']}"
                  if profile.get("wikidata_id") else "")
        if not confirmed:
            not_checked.append("Wikidata: found a similarly named company but could not "
                               "confirm it is this employer, so its facts are not used")
        else:
            employees = profile.get("employees")
            if employees:
                facts += 1
                effect = 2 if employees >= 10000 else 1 if employees >= 1000 else \
                    0 if employees >= 250 else -1
                points += effect
                add("Headcount", f"About {employees:,} staff", effect, "Wikidata", wd_url)
            founded = str(profile.get("founded") or "")
            if re.fullmatch(r"\d{4}", founded) and not register:
                facts += 1
                age = today.year - int(founded)
                effect = 2 if age >= 20 else 1 if age >= 8 else -1 if age < 3 else 0
                points += effect
                add("Founded", f"{founded} ({age} years ago)", effect, "Wikidata", wd_url)

    # --- sponsor register - only a confident name match. An approximate one can be a
    # different business entirely (a San Francisco startup called Flint matched
    # "Flint Wines Ltd."), and crediting its licence would invent a fact.
    rating = (sponsor_rating or "").strip().upper()
    if rating and sponsor_confidence < SPONSOR_MIN_CONFIDENCE:
        not_checked.append(f"Sponsor register: only an approximate name match"
                           f"{f' ({sponsor_name})' if sponsor_name else ''}, so its "
                           f"licence rating is not used")
        rating = ""
    if rating.startswith("A"):
        facts += 1
        points += 1
        add("Visa sponsor licence", "A-rated - in good standing with the Home Office", 1,
            "UK sponsor register",
            "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers")
    elif rating.startswith("B"):
        facts += 1
        points -= 1
        add("Visa sponsor licence", "B-rated - downgraded over a compliance issue", -1,
            "UK sponsor register",
            "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers")

    # --- startup tags from the company registry
    startup = tags.get("startup")
    if startup is True:
        facts += 1
        points -= 1
        add("Startup", tags.get("startup_evidence") or "Tagged as a startup", -1,
            "Company registry")
    elif startup is False and tags.get("startup_evidence"):
        add("Not a startup", tags["startup_evidence"], 0, "Company registry")

    # --- this app's own data
    if activity:
        open_roles = int(activity.get("open_roles") or 0)
        long_open = int(activity.get("long_open") or 0)
        if open_roles >= 10:
            facts += 1
            points += 1
            add("Hiring now", f"{open_roles} open roles found in this search", 1,
                "Job Scout")
        if open_roles >= 3 and long_open / max(open_roles, 1) >= 0.5:
            points -= 1
            add("Slow to fill", f"{long_open} of {open_roles} roles open 45+ days", -1,
                "Job Scout")

    if caution:
        level = "caution"
    elif startup is True:
        level = "early"              # the tag states the stage outright
    elif facts < 2:
        level = "unknown"
    elif points <= 0:
        level = "early"
    elif points >= 4:
        level = "established"
    else:
        level = "stable"

    return {"company": company, "level": level, "label": LEVELS[level],
            "factors": factors, "not_checked": not_checked,
            "startup": startup, "disclaimer": DISCLAIMER}


# ------------------------------------------------------------------ list-view cache
class _LevelCache:
    """Last computed level per company, so the job list can show a pill without a
    network call per row. Written whenever a detail panel computes a rating."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: dict | None = None

    def _load(self) -> dict:
        if self.data is None:
            try:
                self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
            except (OSError, json.JSONDecodeError):
                self.data = {}
        return self.data

    MAX_AGE_DAYS = 7      # hiring activity and register status move; older is not shown

    def get(self, company: str) -> dict | None:
        entry = self._load().get((company or "").strip().lower())
        if not entry or not entry.get("computed_at"):
            return None       # pre-expiry entries count as stale
        try:
            age = date.today() - date.fromisoformat(entry["computed_at"])
        except ValueError:
            return None
        return entry if age.days <= self.MAX_AGE_DAYS else None

    def put(self, company: str, result: dict) -> None:
        data = self._load()
        data[(company or "").strip().lower()] = {"level": result["level"],
                                                 "label": result["label"],
                                                 "computed_at": date.today().isoformat()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), "utf-8")
        except OSError:
            pass


LEVEL_CACHE = _LevelCache()
