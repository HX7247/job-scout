"""Company background, from sources that actually permit programmatic access.

Why not Glassdoor: their public API was shut to new developers in 2022 and is now
enterprise-partner only, so there is no first-party way to read reviews or ratings.
Scraping the site breaches their terms and runs into bot management, which would put
the user's own IP and account at risk. This module therefore does not touch it.

What it uses instead:

  Wikidata  a free, open, no-key API with structured facts on most employers of any
            size - industry, headcount, founding year, headquarters, website. Licensed
            CC0, explicitly built for programmatic reuse.

  the sponsor register  already downloaded for the visa filter, and it carries each
            employer's registered legal name and town, which is useful company detail
            in its own right.

Everything is cached on disk, because company facts change far more slowly than job
postings do.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .http import SESSION

log = logging.getLogger("jobscout.enrich")

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "company_cache.json"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Wikidata property ids we care about.
P_INSTANCE_OF = "P31"
P_INDUSTRY = "P452"
P_EMPLOYEES = "P1128"
P_INCEPTION = "P571"
P_HEADQUARTERS = "P159"
P_WEBSITE = "P856"
P_COUNTRY = "P17"
P_OFFICIAL_NAME = "P1448"      # registered/legal name, e.g. "Monzo Bank Limited"
P_OPENCORPORATES = "P1320"     # "gb/09446231" - jurisdiction and company number
P_LEI = "P1278"                # Legal Entity Identifier
P_STOCK_EXCHANGE = "P414"

# Item ids that mean "this is a company", to avoid matching a person or an album.
BUSINESS_ITEMS = {
    "Q4830453",   # business
    "Q783794",    # company
    "Q6881511",   # enterprise
    "Q891723",    # public company
    "Q210167",    # software company
    "Q43229",     # organization
    "Q167037",    # corporation
    "Q1058914",   # software company (alt)
    "Q18388277",  # technology company
    "Q740752",    # engineering company
}


@dataclass
class CompanyProfile:
    name: str
    wikidata_id: str = ""
    description: str = ""
    industry: list[str] = field(default_factory=list)
    employees: int | None = None
    founded: str = ""
    headquarters: str = ""
    website: str = ""
    country: str = ""
    legal_name: str = ""       # from the sponsor register
    registered_town: str = ""
    official_name: str = ""    # registered name per Wikidata (P1448)
    company_number: str = ""   # e.g. "09446231"
    jurisdiction: str = ""     # e.g. "gb"
    domain: str = ""           # canonical website host, for identity checks
    verified_by: str = ""      # how we know the Wikidata entity is the right one
    source: str = ""

    @property
    def size_band(self) -> str:
        """Rough size, which is what a candidate actually wants to know."""
        n = self.employees
        if not n:
            return ""
        if n < 50:
            return "startup (<50)"
        if n < 250:
            return "small (50-250)"
        if n < 1000:
            return "mid-size (250-1k)"
        if n < 10000:
            return "large (1k-10k)"
        return "very large (10k+)"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["size_band"] = self.size_band
        return data


class _Cache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value) -> None:
        self.data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), "utf-8")
        except OSError:
            pass


_CACHE = _Cache()


def _domain(url: str) -> str:
    """Bare host, so cheeky variations do not defeat a comparison."""
    match = re.search(r"https?://(?:www\.)?([^/?#]+)", url or "", re.I)
    return match.group(1).lower() if match else ""


def _label(entity_ids: list[str]) -> dict[str, str]:
    """Resolve Wikidata item ids to English labels, in one request."""
    if not entity_ids:
        return {}
    data = SESSION.get_json(WIKIDATA_API, params={
        "action": "wbgetentities", "ids": "|".join(entity_ids[:40]),
        "props": "labels", "languages": "en", "format": "json",
    }, check_robots=False)
    out = {}
    for eid, entity in ((data or {}).get("entities") or {}).items():
        label = ((entity.get("labels") or {}).get("en") or {}).get("value")
        if label:
            out[eid] = label
    return out


def _claim_values(claims: dict, prop: str) -> list:
    values = []
    for claim in (claims.get(prop) or []):
        snak = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if snak is not None:
            values.append(snak)
    return values


def from_wikidata(company: str) -> CompanyProfile | None:
    """Look a company up on Wikidata. Returns None when there is no confident match."""
    if not company:
        return None
    search = SESSION.get_json(WIKIDATA_API, params={
        "action": "wbsearchentities", "search": company, "language": "en",
        "type": "item", "limit": 5, "format": "json",
    }, check_robots=False)
    hits = (search or {}).get("search") or []
    if not hits:
        return None

    ids = [h["id"] for h in hits]
    entities = SESSION.get_json(WIKIDATA_API, params={
        "action": "wbgetentities", "ids": "|".join(ids),
        "props": "claims|descriptions|labels", "languages": "en", "format": "json",
    }, check_robots=False)
    if not entities:
        return None

    for eid in ids:                      # search order is relevance order
        entity = (entities.get("entities") or {}).get(eid)
        if not entity:
            continue
        claims = entity.get("claims") or {}
        kinds = {v.get("id") for v in _claim_values(claims, P_INSTANCE_OF)
                 if isinstance(v, dict)}
        if not kinds & BUSINESS_ITEMS:
            # Wikidata has hundreds of company subtypes ("pharmaceutical company",
            # "automotive manufacturer"), so fall back to reading the type's label
            # rather than maintaining an id list that will always be incomplete.
            kind_labels = " ".join(_label(sorted(kinds)).values()).lower()
            if not any(word in kind_labels for word in
                       ("company", "business", "enterprise", "corporation",
                        "manufacturer", "bank", "organisation", "organization",
                        "firm", "retailer", "agency", "startup")):
                continue                  # a person, a place, a song - skip it

        industry_ids = [v["id"] for v in _claim_values(claims, P_INDUSTRY)
                        if isinstance(v, dict) and v.get("id")]
        hq_ids = [v["id"] for v in _claim_values(claims, P_HEADQUARTERS)
                  if isinstance(v, dict) and v.get("id")]
        country_ids = [v["id"] for v in _claim_values(claims, P_COUNTRY)
                       if isinstance(v, dict) and v.get("id")]
        labels = _label(industry_ids + hq_ids + country_ids)

        employees = None
        for value in _claim_values(claims, P_EMPLOYEES):
            if isinstance(value, dict) and value.get("amount"):
                try:
                    employees = int(float(str(value["amount"]).lstrip("+")))
                except ValueError:
                    pass

        founded = ""
        for value in _claim_values(claims, P_INCEPTION):
            if isinstance(value, dict) and value.get("time"):
                match = re.search(r"(\d{4})", value["time"])
                if match:
                    founded = match.group(1)

        websites = [v for v in _claim_values(claims, P_WEBSITE) if isinstance(v, str)]

        # The registered name is what appears on a government register, so it
        # matches a sponsor licence far better than the trading name does.
        official = ""
        for value in _claim_values(claims, P_OFFICIAL_NAME):
            if isinstance(value, dict) and value.get("text"):
                official = value["text"]
                break
            if isinstance(value, str):
                official = value
                break

        company_number = jurisdiction = ""
        for value in _claim_values(claims, P_OPENCORPORATES):
            if isinstance(value, str) and "/" in value:
                jurisdiction, company_number = value.split("/", 1)
                break

        return CompanyProfile(
            name=((entity.get("labels") or {}).get("en") or {}).get("value", company),
            wikidata_id=eid,
            description=((entity.get("descriptions") or {}).get("en") or {}).get("value", ""),
            industry=[labels[i] for i in industry_ids if i in labels][:4],
            employees=employees,
            founded=founded,
            headquarters=", ".join(labels[i] for i in hq_ids if i in labels)[:80],
            website=websites[0] if websites else "",
            official_name=official,
            company_number=company_number,
            jurisdiction=jurisdiction,
            domain=_domain(websites[0]) if websites else "",
            country=", ".join(labels[i] for i in country_ids if i in labels)[:60],
            source="wikidata",
        )
    return None


def company_profile(company: str, sponsor: dict | None = None,
                    refresh: bool = False, job_url: str = "") -> dict | None:
    """Background on an employer, merging Wikidata with the sponsor register.

    When the job's apply URL is on the employer's own domain, it is used to confirm
    the Wikidata entity really is this company - name search alone happily returns
    an unrelated business with a similar name.
    """
    if not company:
        return None
    key = company.strip().lower()
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            profile = dict(cached) if cached else None
            if profile and sponsor:
                profile["legal_name"] = sponsor.get("name", "")
                profile["registered_town"] = sponsor.get("city", "")
            return profile

    try:
        found = from_wikidata(company)
    except Exception as exc:
        log.debug("wikidata lookup failed for %s: %s", company, exc)
        found = None

    if found is None and sponsor:
        # Nothing on Wikidata, but the register still tells us something real.
        found = CompanyProfile(name=company, source="sponsor register")

    if found and job_url:
        posting_domain = _domain(job_url)
        if posting_domain and found.domain:
            root = ".".join(found.domain.split(".")[-2:])
            if root and root in posting_domain:
                found.verified_by = f"apply URL is on {found.domain}"

    if found and sponsor:
        found.legal_name = sponsor.get("name", "")
        found.registered_town = sponsor.get("city", "")

    payload = found.to_dict() if found else {}
    _CACHE.put(key, payload)
    return payload or None
