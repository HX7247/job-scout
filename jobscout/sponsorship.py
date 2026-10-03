"""Visa sponsorship: can this employer actually hire an international candidate?

For an international student this is the single most useful filter there is - most
applications fail not on merit but because the employer holds no sponsor licence.

Data sources are official government registers, not guesses or scraped review sites:

  GB  Home Office "Register of licensed sponsors: workers", published as CSV on GOV.UK
      and refreshed roughly monthly. ~143,000 organisations with their licence rating
      and the routes they may sponsor.
      https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers

  US  Department of Labor OFLC LCA disclosure files - every H-1B/E-3 labour condition
      application, so an employer's filing count is a direct measure of how often they
      actually hire international staff. The quarterly files are large and not served
      at a stable URL, so this module reads one you have downloaded rather than
      fetching it automatically.
      https://www.dol.gov/agencies/eta/foreign-labor

Absence from a register is not proof an employer will not sponsor - small firms apply
for a licence when they find the right person. The UI says "no licence found", never
"will not sponsor".
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz, process

from .http import SESSION

log = logging.getLogger("jobscout.sponsorship")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GB_PUBLICATION = ("https://www.gov.uk/government/publications/"
                  "register-of-licensed-sponsors-workers")

# Legal-form noise only. Words like "consulting", "services" or "technologies" are
# often the distinctive part of a name - stripping them made "Stripe" collide with
# "Stripe Consulting Limited", a completely different company.
_SUFFIXES = r"""
limited ltd plc llp llc lp inc incorporated corp corporation
company co holdings holding gmbh sarl bv nv ag oy ab as
"""
_SUFFIX_RE = re.compile(rf"\b({'|'.join(_SUFFIXES.split())})\b", re.I)
_TRADING_AS = re.compile(r"\b(t/a|trading as|dba)\b.*$", re.I)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

# Routes that matter to someone finishing a degree, in the order they apply.
STUDENT_ROUTES = ["Graduate Trainee", "Skilled Worker", "Global Business Mobility",
                  "Government Authorised Exchange", "Temporary Worker"]


def normalise(name: str) -> str:
    """Company name reduced to its distinctive core, for matching."""
    text = _TRADING_AS.sub(" ", (name or "").lower())
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _SUFFIX_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


@dataclass
class SponsorRecord:
    name: str
    city: str = ""
    county: str = ""
    ratings: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    match: str = "exact"        # exact | partial | fuzzy
    confidence: float = 1.0
    alternatives: list[str] = field(default_factory=list)

    @property
    def rating_letter(self) -> str:
        """A, B or provisional - A is a sponsor in good standing."""
        joined = " ".join(self.ratings)
        if "A rating" in joined or "A (" in joined:
            return "A"
        if "B rating" in joined:
            return "B"
        if "Provisional" in joined:
            return "Provisional"
        return "?"

    @property
    def student_relevant(self) -> bool:
        return any(any(r.lower() in route.lower() for r in STUDENT_ROUTES)
                   for route in self.routes)

    def to_dict(self) -> dict:
        return {"name": self.name, "city": self.city, "county": self.county,
                "ratings": self.ratings, "routes": self.routes,
                "rating_letter": self.rating_letter, "match": self.match,
                "confidence": round(self.confidence, 3),
                "alternatives": self.alternatives,
                "ambiguous": bool(self.alternatives),
                "student_relevant": self.student_relevant}


class SponsorRegister:
    """Loads a national sponsor register and answers 'can they sponsor?'."""

    def __init__(self, country: str = "GB"):
        self.country = (country or "GB").upper()
        self.path = DATA_DIR / f"sponsors_{self.country}.csv"
        self.by_key: dict[str, SponsorRecord] = {}
        self.keys: list[str] = []
        self.loaded_at: str | None = None
        # First token -> candidate keys, so fuzzy matching never scans all 143k.
        self._by_token: dict[str, list[str]] = defaultdict(list)

    # ------------------------------------------------------------- acquisition
    def _discover_gb_csv(self) -> str | None:
        """The GOV.UK filename carries its publication date, so find it each time."""
        html = SESSION.get_text(GB_PUBLICATION, check_robots=False)
        if not html:
            return None
        links = re.findall(
            r'https://assets\.publishing\.service\.gov\.uk/media/[^"\']+?\.csv', html)
        worker = [l for l in links if "Worker" in l or "worker" in l]
        return (worker or links or [None])[0]

    def download(self, force: bool = False) -> bool:
        """Fetch the register if we do not already have a copy."""
        if self.path.exists() and not force:
            return True
        if self.country != "GB":
            log.warning("no automatic download for %s - drop a CSV at %s",
                        self.country, self.path)
            return False
        url = self._discover_gb_csv()
        if not url:
            log.error("could not find the sponsor register CSV on GOV.UK")
            return False
        log.info("downloading sponsor register: %s", url)
        text = SESSION.get_text(url, check_robots=False)
        if not text:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, "utf-8")
        return True

    # ------------------------------------------------------------------ load
    def load(self, download_if_missing: bool = True) -> bool:
        if self.by_key:
            return True
        if not self.path.exists():
            if not download_if_missing or not self.download():
                return False
        try:
            text = self.path.read_text("utf-8-sig", errors="replace")
        except OSError as exc:
            log.error("cannot read %s: %s", self.path, exc)
            return False

        reader = csv.DictReader(io.StringIO(text))
        merged: dict[str, SponsorRecord] = {}
        for row in reader:
            name = (row.get("Organisation Name") or row.get("Employer") or "").strip()
            if not name:
                continue
            key = normalise(name)
            if not key:
                continue
            record = merged.get(key)
            if record is None:
                record = SponsorRecord(
                    name=name,
                    city=(row.get("Town/City") or "").strip(),
                    county=(row.get("County") or "").strip(),
                )
                merged[key] = record
            rating = (row.get("Type & Rating") or "").strip()
            route = (row.get("Route") or "").strip()
            if rating and rating not in record.ratings:
                record.ratings.append(rating)
            if route and route not in record.routes:
                record.routes.append(route)

        self.by_key = merged
        self.keys = list(merged)
        self._by_token.clear()
        # Index on every token, not just the first: employers trade as "Arup" but
        # register as "Ove Arup & Partners", so the distinctive word can be anywhere.
        for key in self.keys:
            for token in set(key.split()):
                if len(token) > 2:
                    self._by_token[token].append(key)
        self.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.info("sponsor register loaded: %d organisations", len(self.by_key))
        return True

    # ----------------------------------------------------------------- lookup
    def lookup(self, company: str, min_confidence: float = 0.90,
               location_hint: str = "",
               aliases: list[str] | None = None) -> SponsorRecord | None:
        """Try the trading name, then any registered names we were given.

        A registered name from an official source ("Monzo Bank Limited") matches a
        government register far more precisely than a trading name ("Monzo"), so an
        exact hit on an alias beats a partial hit on the trading name.
        """
        best: SponsorRecord | None = None
        for index, candidate in enumerate([company] + list(aliases or [])):
            found = self._lookup_one(candidate, min_confidence, location_hint)
            if found is None:
                continue
            if index > 0 and found.match == "exact":
                # An exact hit on the registered name is the strongest evidence
                # available, so take it and stop.
                found.match = "registered name"
                found.alternatives = []
                return found
            if best is None or found.confidence > best.confidence:
                best = found
        return best

    def _lookup_one(self, company: str, min_confidence: float = 0.90,
                    location_hint: str = "") -> SponsorRecord | None:
        """Exact match on the normalised name, then a bounded fuzzy fallback."""
        if not company or not self.load():
            return None
        key = normalise(company)
        if not key:
            return None

        record = self.by_key.get(key)
        if record:
            return record

        # Employers usually trade under a shorter name than they registered
        # ("Monzo" vs "MONZO BANK LIMITED"), so look for the query's tokens as a
        # subset of a registered name rather than a whole-string similarity.
        query_tokens = [t for t in key.split() if len(t) > 2]
        if not query_tokens:
            return None

        hint = (location_hint or "").split(",")[0].strip().lower()
        candidates: set[str] = set()
        for token in query_tokens:
            candidates.update(self._by_token.get(token, ())[:400])
        if not candidates:
            return None

        wanted = set(query_tokens)
        scored: list[tuple[float, str]] = []
        for candidate in candidates:
            candidate_tokens = set(candidate.split())
            if not wanted <= candidate_tokens:
                continue
            # Every query word is present. Confidence falls the more extra words the
            # registered name carries, so "monzo" -> "monzo bank" beats
            # "monzo" -> "monzo bank holdings trading company".
            conf = len(wanted) / max(len(candidate_tokens), 1)
            # The register records each sponsor's town. When the job names a place
            # too, a matching town separates the engineering "Atkins" from the
            # unrelated bricklaying one.
            if hint and hint in (self.by_key[candidate].city or "").lower():
                conf += 0.30
            scored.append((conf, candidate))

        if scored:
            scored.sort(reverse=True)
            best_conf, best_key = scored[0]
            if best_conf >= 0.45:
                found = self.by_key[best_key]
                # Any other registered name that also contains every query word is a
                # genuine rival, even if it scores lower: "Stripe" fits both "Stripe
                # Partners" and "Stripe Consulting Limited", and neither is Stripe Inc.
                rivals = [self.by_key[k].name for _, k in scored[1:6]]
                # A subset match is inference, not identification. Cap it below the
                # level an exact match earns so the UI never presents it as settled.
                confidence = min(best_conf, 0.85)
                if rivals:
                    confidence = min(confidence, 0.6)
                return SponsorRecord(
                    name=found.name, city=found.city, county=found.county,
                    ratings=found.ratings, routes=found.routes,
                    match="partial", confidence=confidence,
                    alternatives=rivals,
                )

        # Last resort: tolerate spelling and word-order differences.
        best = process.extractOne(key, list(candidates), scorer=fuzz.token_set_ratio)
        if not best or best[1] / 100.0 < min_confidence:
            return None
        found = self.by_key[best[0]]
        return SponsorRecord(
            name=found.name, city=found.city, county=found.county,
            ratings=found.ratings, routes=found.routes,
            match="fuzzy", confidence=best[1] / 100.0,
        )

    def status(self) -> dict:
        return {
            "country": self.country,
            "available": self.path.exists(),
            "organisations": len(self.by_key),
            "path": str(self.path),
            "loaded_at": self.loaded_at,
            "source": GB_PUBLICATION if self.country == "GB" else "manual CSV",
        }


COMPANIES_HOUSE_API = "https://api.company-information.service.gov.uk"


def registered_name_for_number(company_number: str) -> str:
    """Authoritative registered name for a UK company number.

    Needs a free Companies House API key in COMPANIES_HOUSE_KEY. This is the only
    way to identify an employer with certainty: names collide, numbers do not.
    Without a key we fall back to name matching and say so in the UI.
    """
    import os

    key = os.environ.get("COMPANIES_HOUSE_KEY")
    if not key or not company_number:
        return ""
    import base64

    token = base64.b64encode(f"{key}:".encode()).decode()
    data = SESSION.get_json(
        f"{COMPANIES_HOUSE_API}/company/{company_number.strip()}",
        headers={"Authorization": f"Basic {token}"}, check_robots=False)
    return (data or {}).get("company_name", "") or ""


def verification_available() -> bool:
    import os
    return bool(os.environ.get("COMPANIES_HOUSE_KEY"))


# One shared register per country, so the 143k-row parse happens once per process.
_REGISTERS: dict[str, SponsorRegister] = {}


def register_for(country: str | None) -> SponsorRegister | None:
    """Get the register for a country, or None where we have no data for it."""
    code = (country or "").upper()
    if code not in ("GB", "US"):
        return None
    if code not in _REGISTERS:
        _REGISTERS[code] = SponsorRegister(code)
    return _REGISTERS[code]


def check(company: str, country: str | None, location: str = "",
          aliases: list[str] | None = None) -> dict | None:
    """Convenience wrapper used by scoring and the API."""
    register = register_for(country)
    if register is None:
        return None
    record = register.lookup(company, location_hint=location, aliases=aliases)
    return record.to_dict() if record else None
