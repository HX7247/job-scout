"""Find more company career boards: graduate employers AND hiring startups.

Two candidate pools, one verification pass:

1. CURATED  - UK graduate / placement / internship employers (plus a smaller US and
   Australian set). Workday employers are listed with their real public
   "tenant:wdN:site" (taken from links on their careers pages / job adverts - none of
   the three parts is guessable); simple-GET ATS employers get a few slug guesses.
2. YC       - the public, MIT-licensed YC company dataset published by the yc-oss/api
   project (https://github.com/yc-oss/api), filtered to active companies flagged as
   hiring, UK/Europe first, then remote, then US. Slugs come from the company name
   AND its website domain.

Every candidate is probed against the official public ATS JSON endpoints (same
PROBES as tools/discover_slugs.py). A company is kept only when a probe returns at
least one real posting AND, where the ATS exposes the board owner's name (or the
postings mention it), that name matches - so a small startup does not inherit some
unrelated employer's board through a slug collision.

Output (never touches data/companies_verified.yaml, which the live scraper reads):
    data/companies_extra.yaml  - new boards, registry schema + tag fields
    data/company_tags.yaml     - startup tags for companies already in the registry

Tag rule: startup: true only when founded within ~12 years AND fewer than ~500 staff
are both actually known; established employers are startup: false; unknown -> the
field is omitted. The yc-oss data has no founding year, so the YC batch year stands in
for it (a company is at least as old as its batch; field `yc_batch`, `founded` left
out).

Run:  python tools/discover_startups.py                 # everything, <=2,500 requests
      python tools/discover_startups.py --skip-yc       # curated employers only
      python tools/discover_startups.py --yc-limit 600 --max-requests 4000
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - older consoles
    pass

# Reuse the simple-GET probe table and slug helper from the original discovery tool.
from discover_slugs import HEADERS as _BASE_HEADERS, PROBES, TIMEOUT, slug_variants  # noqa: E402

HEADERS = {"User-Agent": "JobScout/0.1 (personal job-search assistant; "
                         "careers-board discovery)",
           **{k: v for k, v in _BASE_HEADERS.items() if k != "User-Agent"}}
YC_META_URL = "https://yc-oss.github.io/api/meta.json"
YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
YC_ALL_URL = "https://yc-oss.github.io/api/companies/all.json"

REGISTRY = ROOT / "data" / "companies_verified.yaml"
OUT_EXTRA = ROOT / "data" / "companies_extra.yaml"
OUT_TAGS = ROOT / "data" / "company_tags.yaml"

THIS_YEAR = dt.date.today().year
STARTUP_MAX_AGE = 12
STARTUP_MAX_STAFF = 500

# Startups almost never use SmartRecruiters; skipping it saves ~1/6 of YC probes.
YC_ATS_ORDER = ["ashby", "greenhouse", "lever", "workable", "recruitee"]
CURATED_ATS_ORDER = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"]


# --------------------------------------------------------------------------- budget
class Budget:
    """Thread-safe request cap so a run can never hammer the ATS endpoints."""

    def __init__(self, cap: int):
        self.cap, self.used, self._lock = cap, 0, threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.cap:
                return False
            self.used += 1
            return True


BUDGET = Budget(2500)


# --------------------------------------------------------------------------- probes
def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _get(url: str):
    if not BUDGET.take():
        raise RuntimeError("budget")
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def probe_simple(ats: str, slug: str) -> dict | None:
    """One GET against a simple ATS board. Returns count, sample title, owner signal."""
    url_tpl, key = PROBES[ats]
    payload = _get(url_tpl.format(slug=slug))
    if payload is None:
        return None
    items = payload if key is None else (payload.get(key) if isinstance(payload, dict) else None)
    if not isinstance(items, list) or not items:
        return None
    count = len(items)
    owner, text, title = "", "", ""
    first = items[0] if isinstance(items[0], dict) else {}
    if ats == "greenhouse":
        owner, title = first.get("company_name", ""), first.get("title", "")
    elif ats == "workable":
        owner, title = payload.get("name", ""), first.get("title", "")
    elif ats == "recruitee":
        owner, title = first.get("company_name", ""), first.get("title", "")
    elif ats == "smartrecruiters":
        owner, title = (first.get("company") or {}).get("name", ""), first.get("name", "")
        count = int(payload.get("totalFound") or count)
    elif ats == "lever":
        title = first.get("text", "")
        text = " ".join((i.get("descriptionPlain") or "") + " " + (i.get("additionalPlain") or "")
                        for i in items[:3] if isinstance(i, dict))
    elif ats == "ashby":
        title = first.get("title", "")
        text = " ".join((i.get("descriptionPlain") or "") for i in items[:3] if isinstance(i, dict))
    return {"count": count, "title": title, "owner": owner, "text": text}


def probe_workday(slug: str) -> dict | None:
    tenant, host, site = slug.split(":")
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    if not BUDGET.take():
        raise RuntimeError("budget")
    try:
        resp = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                             json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                   "searchText": ""}, timeout=TIMEOUT + 5)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    posts = data.get("jobPostings") or []
    total = int(data.get("total") or len(posts))
    if not posts or total <= 0:
        return None
    return {"count": total, "title": posts[0].get("title", ""), "owner": "", "text": ""}


def name_check(company: str, hit: dict, extra_terms: list[str] = ()) -> str:
    """'match' | 'mismatch' | 'unverified' - does the board belong to this company?"""
    want = norm(company)
    terms = [t for t in {want, *map(norm, extra_terms)} if len(t) >= 4]
    first_word = norm(re.split(r"[\s\-.]+", company.strip())[0]) if company.strip() else ""
    if hit.get("owner"):
        owner = norm(hit["owner"])
        if want and (want in owner or owner in want):
            return "match"
        if len(first_word) >= 4 and first_word in owner:
            return "match"
        if any(t in owner for t in terms):
            return "match"
        return "mismatch"
    body = norm(hit.get("text", ""))
    if body and any(t in body for t in terms):
        return "match"
    return "unverified"


def find_simple(company: str, slugs: list[str], order: list[str],
                extra_terms: list[str] = ()) -> tuple[dict | None, list[str]]:
    """Try slug x ATS; first board with postings whose owner is not someone else wins."""
    notes = []
    for slug in slugs:
        for ats in order:
            try:
                hit = probe_simple(ats, slug)
            except RuntimeError:
                notes.append("budget exhausted")
                return None, notes
            except Exception:
                continue
            if not hit:
                continue
            verdict = name_check(company, hit, extra_terms)
            if verdict == "mismatch":
                notes.append(f"{ats}:{slug} belongs to '{hit['owner']}'")
                continue
            return {"ats": ats, "slug": slug, "jobs_seen": hit["count"],
                    "sample_title": hit["title"], "name_check": verdict}, notes
    return None, notes


# --------------------------------------------------------------------------- curated
# (company, "tenant:wdN:site", evidence, market). Sites were taken from real postings
# on each employer's myworkdayjobs.com board; early-career sites preferred where the
# employer runs a separate one, because the adapter reads at most max_per_company.
CURATED_WORKDAY: list[tuple[str, str, str, str]] = [
    # UK - banking, finance, insurance, regulators
    ("Barclays", "barclays:wd3:External_Career_Site_Barclays", "FTSE 100 bank", "UK"),
    ("Lloyds Banking Group", "lbg:wd3:Graduate_careers", "FTSE 100 bank - graduate site", "UK"),
    ("Lloyds Banking Group", "lbg:wd3:LBG_Careers", "FTSE 100 bank", "UK"),
    ("NatWest Group", "rbs:wd3:RBS", "FTSE 100 bank", "UK"),
    ("Santander", "santander:wd3:SantanderCareers", "global bank", "UK"),
    ("LSEG", "lseg:wd3:Graduate_Careers", "FTSE 100 exchange group - graduate site", "UK"),
    ("LSEG", "lseg:wd3:Careers", "FTSE 100 exchange group", "UK"),
    ("Deutsche Bank", "db:wd3:DBWebsite", "global bank", "UK"),
    ("Morgan Stanley", "ms:wd5:External", "global investment bank", "UK"),
    ("Citi", "citi:wd5:2", "global bank", "UK"),
    ("BlackRock", "blackrock:wd1:BlackRock_Professional", "global asset manager", "UK"),
    ("Blackstone", "blackstone:wd1:Blackstone_Campus_Careers", "global asset manager - campus site", "UK"),
    ("Houlihan Lokey", "hl:wd1:Campus", "investment bank - campus site", "UK"),
    ("PJT Partners", "pjtpartners:wd1:Students", "investment bank - students site", "UK"),
    ("BBVA", "bbva:wd3:BBVA", "global bank", "UK"),
    ("Capital One", "capitalone:wd12:Capital_One", "US bank with UK graduate scheme", "UK"),
    ("Mastercard", "mastercard:wd1:Campus", "payments network - campus site", "UK"),
    ("Mastercard", "mastercard:wd1:CorporateCareers", "payments network", "UK"),
    ("AIG", "aig:wd1:early_careers", "global insurer - early careers site", "UK"),
    ("Howden", "hyperiongrp:wd3:Hyperion_External", "insurance broker group", "UK"),
    ("NewDay", "newday:wd3:NewDay", "UK consumer credit provider", "UK"),
    ("Financial Conduct Authority", "fca:wd3:FCA_earlycareers", "UK regulator - early careers site", "UK"),
    ("Financial Conduct Authority", "fca:wd3:fca_careers", "UK regulator", "UK"),
    ("Ofcom", "ofcom:wd3:Ofcom_Careers", "UK regulator", "UK"),
    ("G-Research", "gresearch:wd103:G-Research", "quant research firm", "UK"),
    ("JLL", "jll:wd1:jllcareers", "global real-estate services", "UK"),
    ("Norton Rose Fulbright", "nrf:wd3:Graduates", "global law firm - graduates site", "UK"),
    ("PwC", "pwc:wd3:Global_Campus_Careers", "Big Four - campus site", "UK"),
    ("Ankura", "ankura:wd5:Ankura", "consulting firm", "UK"),
    # UK - engineering, aerospace, defence, energy, manufacturing
    ("Rolls-Royce", "rollsroyce:wd3:Intern_Graduate", "FTSE 100 engineer - intern & graduate site", "UK"),
    ("Rolls-Royce", "rollsroyce:wd3:professional", "FTSE 100 engineer", "UK"),
    ("MBDA", "mbda:wd3:MBDA-UK", "missile systems, UK site", "UK"),
    ("Airbus", "ag:wd3:Airbus", "aerospace manufacturer", "UK"),
    ("AWE", "awepeople:wd3:Grad_Careers", "UK defence establishment - graduate site", "UK"),
    ("Mercedes-AMG Petronas F1", "mbgp:wd3:Mercedes-AMGF1", "Formula 1 team", "UK"),
    ("AtkinsRealis", "slihrms:wd3:Careers", "engineering consultancy", "UK"),
    ("Weir Group", "weir:wd3:Weir_External_Careers", "FTSE engineering group", "UK"),
    ("Johnson Matthey", "matthey:wd3:Ext_Career_Site", "FTSE chemicals group", "UK"),
    ("bp", "bpinternational:wd3:bpCareers", "FTSE 100 energy major", "UK"),
    ("Centrica", "centrica:wd3:Centrica", "FTSE 100 energy group", "UK"),
    ("Equinor", "equinor:wd3:EQNR", "energy major", "UK"),
    ("Arriva", "arriva:wd3:Careers", "transport operator", "UK"),
    # UK - pharma, FMCG, consumer, tech
    ("AstraZeneca", "astrazeneca:wd3:Emerging-Talent", "FTSE 100 pharma - emerging talent site", "UK"),
    ("Haleon", "gsknch:wd3:GSKCareers", "FTSE 100 consumer health", "UK"),
    ("ViiV Healthcare", "gsk:wd5:ViiV_Careers", "GSK-majority pharma", "UK"),
    ("Viatris", "viatris:wd5:External", "global pharma", "UK"),
    ("Unilever", "unilever:wd3:Unilever_Early_Careers", "FTSE 100 FMCG - early careers site", "UK"),
    ("Unilever", "unilever:wd3:Unilever_Experienced_Professionals", "FTSE 100 FMCG", "UK"),
    ("Procter & Gamble", "pg:wd5:1000", "global FMCG", "UK"),
    ("Mondelez", "mdlz:wd3:External", "global FMCG", "UK"),
    ("Uniqlo (Fast Retailing)", "fastretailing:wd3:graduates_eu_Uniqlo", "global retailer - EU graduate site", "UK"),
    ("Sony", "sonyglobal:wd1:Sony_Europe_Careers", "global electronics - Europe site", "UK"),
    ("Autodesk", "autodesk:wd1:uni", "software company - university site", "UK"),
    ("RELX", "relx:wd3:relx", "FTSE 100 information group", "UK"),
    ("Thomson Reuters", "thomsonreuters:wd5:External_Career_Site", "information group", "UK"),
    ("Kyndryl", "kyndryl:wd5:KyndrylEarlyCareers", "IT services - early careers site", "UK"),
    ("Workday", "workday:wd5:Workday_Early_Career", "enterprise software - early careers site", "UK"),
    ("Abbott", "abbott:wd5:abbottcareers", "global healthcare", "UK"),
    # US
    ("PwC US", "pwc:wd3:US_Entry_Level_Careers", "Big Four - US entry-level site", "US"),
    ("Nvidia", "nvidia:wd5:NVIDIAExternalCareerSite", "semiconductor company", "US"),
    ("Salesforce", "salesforce:wd12:External_Career_Site", "enterprise software", "US"),
    ("Adobe", "adobe:wd5:external_experienced", "software company", "US"),
    ("Intel", "intel:wd1:External", "semiconductor company", "US"),
    ("HP", "hp:wd5:ExternalCareerSite", "computing hardware", "US"),
    ("Boeing", "boeing:wd1:INTERN", "aerospace - intern site", "US"),
    ("Boeing", "boeing:wd1:EXTERNAL_CAREERS", "aerospace manufacturer", "US"),
    ("Northrop Grumman", "ngc:wd1:Northrop_Grumman_External_Site", "defence contractor", "US"),
    ("RTX", "globalhr:wd5:REC_RTX_Ext_Gateway", "aerospace & defence", "US"),
    ("NASA JPL", "citjpl:wd5:Jobs", "NASA research centre (Caltech)", "US"),
    ("General Motors", "generalmotors:wd5:Careers_GM", "automaker", "US"),
    ("Caterpillar", "cat:wd5:CaterpillarCareers", "heavy equipment manufacturer", "US"),
    ("Pfizer", "pfizer:wd1:PfizerCareers", "global pharma", "US"),
    ("Bristol Myers Squibb", "bristolmyerssquibb:wd5:BMS", "global pharma", "US"),
    ("Merck (MSD)", "msd:wd5:SearchJobs", "global pharma", "US"),
    ("Johnson & Johnson", "jj:wd5:JJ", "global healthcare", "US"),
    ("Amgen", "amgen:wd1:Careers", "biotech", "US"),
    ("Moderna", "modernatx:wd1:M_tx", "biotech", "US"),
    ("Medtronic", "medtronic:wd1:MedtronicCareers", "medical devices", "US"),
    ("Novartis", "novartis:wd3:Novartis_Careers", "global pharma", "US"),
    ("Sanofi", "sanofi:wd3:SanofiCareers", "global pharma", "US"),
    ("Coca-Cola", "coke:wd1:coca-cola-careers", "global FMCG", "US"),
    ("Bank of America", "ghr:wd1:Lateral-US", "US bank", "US"),
    ("Marsh McLennan", "mmc:wd1:MMC", "professional services", "US"),
    ("S&P Global", "spgi:wd5:SPGI_Careers", "ratings & data", "US"),
    ("Northern Trust", "ntrs:wd1:northerntrust", "bank & asset servicer", "US"),
    ("Nasdaq", "nasdaq:wd1:Global_External_Site", "exchange operator", "US"),
    # Australia
    ("Commonwealth Bank", "cba:wd3:CommBank_Careers", "ASX-listed bank", "AU"),
    ("NAB", "nab:wd3:nab_careers", "ASX-listed bank", "AU"),
    ("Telstra", "telstra:wd3:Telstra_Careers", "ASX-listed telco", "AU"),
    ("Macquarie Group", "mq:wd3:CareersatMQ", "ASX-listed investment bank", "AU"),
    ("nbn co", "nbn:wd3:nbncareers", "Australian government network company", "AU"),
    ("Glencore Coal Assets Australia", "gcaa:wd3:GCAA_Careers", "mining", "AU"),
    ("KBR", "kbr:wd5:KBR_Careers", "engineering services", "AU"),
    ("DXC Technology", "dxctechnology:wd1:DXCJobs", "IT services", "AU"),
]

# (company, [slug guesses], market, startup, evidence, founded, team_size)
# startup/founded/team_size are None when not known confidently.
CURATED_SIMPLE: list[tuple] = [
    # Quant / trading - big graduate and intern intakes, London offices
    ("IMC Trading", ["imc"], "UK", False, "established trading firm (1989)", 1989, None),
    ("DRW", ["drweng", "drw"], "UK", False, "established trading firm (1992)", 1992, None),
    ("Squarepoint Capital", ["squarepointcapital"], "UK", False, "established hedge fund (2014, 1,000+ staff)", 2014, None),
    ("XTX Markets", ["xtxmarketstechnologies", "xtxmarkets"], "UK", None, "", 2015, None),
    ("Old Mission", ["oldmissioncapital"], "US", False, "established trading firm (2008)", 2008, None),
    ("Hudson River Trading", ["wehrtyou", "hudsonrivertrading"], "US", False, "established trading firm (2002)", 2002, None),
    ("Tower Research Capital", ["towerresearchcapital"], "US", False, "established trading firm (1998)", 1998, None),
    ("Akuna Capital", ["akunacapital"], "US", False, "established trading firm (2011)", 2011, None),
    ("Optiver", ["optiver", "optiverus"], "UK", False, "established trading firm (1986)", 1986, None),
    ("Five Rings", ["fiveringsllc", "fiverings"], "US", None, "", None, None),
    ("Marshall Wace", ["marshallwace"], "UK", False, "established hedge fund (1997)", 1997, None),
    ("Qube Research & Technologies", ["qube", "qubert", "qube-research-technologies"], "UK", None, "", None, None),
    ("Maven Securities", ["mavensecurities"], "UK", None, "", None, None),
    ("Da Vinci Trading", ["davincitrading", "davinci"], "UK", None, "", None, None),
    ("Quadrature Capital", ["quadrature", "quadraturecapital"], "UK", None, "", None, None),
    ("Arrowstreet Capital", ["arrowstreetcapital"], "US", False, "established asset manager (1999)", 1999, None),
    ("Virtu Financial", ["virtu", "virtufinancial"], "US", False, "listed trading firm (2008)", 2008, None),
    # Tech with graduate / intern schemes
    ("Google DeepMind", ["deepmind"], "UK", False, "Alphabet subsidiary", None, None),
    ("Isomorphic Labs", ["isomorphiclabs"], "UK", None, "", 2021, None),
    ("Spotify", ["spotify"], "UK", False, "listed company (2006)", 2006, None),
    ("Skyscanner", ["skyscanner"], "UK", False, "established company (2003)", 2003, None),
    ("Snowflake", ["snowflake", "snowflakecomputing"], "US", False, "listed company (2012)", 2012, None),
    ("Samsara", ["samsara"], "US", False, "listed company (2015, 3,000+ staff)", 2015, None),
    ("Dropbox", ["dropbox"], "US", False, "listed company (YC S07)", 2007, None),
    ("Pinterest", ["pinterest"], "US", False, "listed company (2010)", 2010, None),
    ("Airbnb", ["airbnb"], "US", False, "listed company (YC W09)", 2008, None),
    ("Lyft", ["lyft"], "US", False, "listed company (2012)", 2012, None),
    ("Roblox", ["roblox"], "US", False, "listed company (2004)", 2004, None),
    ("Epic Games", ["epicgames"], "US", False, "established company (1991)", 1991, None),
    ("Instacart", ["instacart"], "US", False, "listed company (YC S12)", 2012, None),
    ("Okta", ["okta"], "US", False, "listed company (2009)", 2009, None),
    ("Affirm", ["affirm"], "US", False, "listed company (2012)", 2012, None),
    ("SoFi", ["sofi"], "US", False, "listed company (2011)", 2011, None),
    ("Duolingo", ["duolingo"], "US", False, "listed company (2011)", 2011, None),
    ("Waymo", ["waymo"], "US", False, "Alphabet subsidiary", None, None),
    ("Anduril", ["anduril", "andurilindustries"], "US", False, "defence tech (2017, 4,000+ staff)", 2017, None),
    ("Canonical", ["canonical"], "UK", False, "established company (2004)", 2004, None),
    ("Toast", ["toast"], "US", False, "listed company (2011)", 2011, None),
    ("Chime", ["chime"], "US", False, "listed company (2012)", 2012, None),
    ("Atlassian", ["atlassian"], "AU", False, "listed company (2002)", 2002, None),
    ("CrowdStrike", ["crowdstrike"], "US", False, "listed company (2011)", 2011, None),
    # UK scale-ups / deep tech / engineering with placement or grad intakes
    ("Octopus Energy", ["octopusenergy", "octopus-energy"], "UK", False, "founded 2015, thousands of staff", 2015, None),
    ("Kraken Technologies", ["kraken", "krakentechnologies"], "UK", None, "", None, None),
    ("Ocado Group", ["ocadogroup", "ocado"], "UK", False, "listed company (2000)", 2000, None),
    ("Trainline", ["trainline"], "UK", False, "listed company (1997)", 1997, None),
    ("Motorway", ["motorway"], "UK", None, "", 2017, None),
    ("Zego", ["zego"], "UK", None, "", 2016, None),
    ("CMR Surgical", ["cmrsurgical"], "UK", False, "founded 2014, ~700 staff", 2014, None),
    ("Riverlane", ["riverlane"], "UK", True, "founded 2016, ~150 staff (curated estimate)", 2016, None),
    ("Quantinuum", ["quantinuum"], "UK", None, "", 2021, None),
    ("PhysicsX", ["physicsx"], "UK", True, "founded 2019, <300 staff (curated estimate)", 2019, None),
    ("Helsing", ["helsing"], "UK", None, "", 2021, None),
    ("Tokamak Energy", ["tokamakenergy"], "UK", None, "", 2009, None),
    ("First Light Fusion", ["firstlightfusion"], "UK", None, "", 2011, None),
    ("Luminance", ["luminance"], "UK", None, "", 2015, None),
    ("Nothing", ["nothing", "nothingtechnology"], "UK", None, "", 2020, None),
    ("Gousto", ["gousto"], "UK", False, "founded 2012, ~1,000+ staff", 2012, None),
    ("Cambridge Consultants", ["cambridgeconsultants"], "UK", False, "established consultancy (1960)", 1960, None),
    ("Frazer-Nash Consultancy", ["frazernash", "frazer-nash"], "UK", False, "established consultancy", None, None),
    ("Newton Europe", ["newtoneurope", "newton"], "UK", False, "established consultancy (2001)", 2001, None),
    ("Renishaw", ["renishaw"], "UK", False, "listed engineer (1973)", 1973, None),
    ("Imagination Technologies", ["imaginationtechnologies", "imagination"], "UK", False, "established chip designer (1985)", 1985, None),
    ("Arm", ["arm", "armltd"], "UK", False, "listed chip designer (1990)", 1990, None),
]

# Tags for companies already in data/companies_verified.yaml that are NOT in the YC
# dataset. Only entries I am confident about; anything unsure is simply absent.
# (startup, evidence, founded)
EXISTING_TAGS: dict[str, tuple] = {
    "Accenture": (False, "global consultancy, ~750k staff", None),
    "Airtable": (False, "founded 2012, ~1,000 staff", 2012),
    "Anthropic": (False, "founded 2021, 1,000+ staff", 2021),
    "Asana": (False, "listed company, founded 2008", 2008),
    "Asda": (False, "UK supermarket chain", None),
    "AstraZeneca": (False, "FTSE 100 pharma", None),
    "Aviva": (False, "FTSE 100 insurer", None),
    "BCG": (False, "global consultancy (1963)", 1963),
    "BP": (False, "FTSE 100 energy major", None),
    "Bupa": (False, "established health insurer (1947)", 1947),
    "Caffe Nero": (False, "UK coffee chain (1997)", 1997),
    "Canva": (False, "founded 2012, ~5,000 staff", 2012),
    "Checkout.com": (False, "founded 2012, ~1,700 staff", 2012),
    "Cisco": (False, "listed company (1984)", 1984),
    "Cloudflare": (False, "listed company (2009)", 2009),
    "Databricks": (False, "founded 2013, 7,000+ staff", 2013),
    "Datadog": (False, "listed company (2010)", 2010),
    "Deliveroo": (False, "founded 2013, 2,000+ staff", 2013),
    "Diageo": (False, "FTSE 100 drinks group", None),
    "Discord": (False, "founded 2015, ~800 staff", 2015),
    "Dominos": (False, "established pizza chain", None),
    "DPD": (False, "established parcel carrier", None),
    "Dyson": (False, "established engineer (1991)", 1991),
    "Elastic": (False, "listed company (2012)", 2012),
    "EY": (False, "Big Four professional services", None),
    "Figma": (False, "listed company, founded 2012", 2012),
    "Flow Traders": (False, "listed trading firm (2004)", 2004),
    "GoCardless": (False, "founded 2011, ~800 staff", 2011),
    "Greene King": (False, "established pub group (1799)", 1799),
    "Griffin": (True, "founded 2017, ~150 staff (curated estimate)", 2017),
    "GSK": (False, "FTSE 100 pharma", None),
    "Improbable": (False, "founded 2012", 2012),
    "Jane Street": (False, "established trading firm (2000)", 2000),
    "JD Sports": (False, "FTSE 100 retailer", None),
    "Jump Trading": (False, "established trading firm (1999)", 1999),
    "Linear": (True, "founded 2019, ~150 staff (curated estimate)", 2019),
    "Lush": (False, "established retailer (1995)", 1995),
    "Man Group": (False, "listed asset manager", None),
    "Matillion": (False, "founded 2011", 2011),
    "Miro": (False, "founded 2011, 1,500+ staff", 2011),
    "MongoDB": (False, "listed company (2007)", 2007),
    "Monzo": (False, "founded 2015, 3,000+ staff", 2015),
    "Mott MacDonald": (False, "established engineering consultancy", None),
    "Notion": (False, "founded 2013, ~1,000 staff", 2013),
    "OpenAI": (False, "founded 2015, 3,000+ staff", 2015),
    "Paddle": (False, "founded 2012", 2012),
    "Palantir": (False, "listed company (2003)", 2003),
    "Plaid": (False, "founded 2013, ~1,000 staff", 2013),
    "Point72": (False, "established hedge fund", None),
    "PolyAI": (True, "founded 2017, ~250 staff (curated estimate)", 2017),
    "Pret A Manger": (False, "established food chain (1986)", 1986),
    "Primark": (False, "established retailer (1969)", 1969),
    "PureGym": (False, "established gym chain (2009)", 2009),
    "Quantexa": (False, "founded 2016, 700+ staff", 2016),
    "Ramp": (False, "founded 2019, 1,000+ staff", 2019),
    "Robinhood": (False, "listed company (2013)", 2013),
    "Sentry": (False, "founded 2012", 2012),
    "Shell": (False, "FTSE 100 energy major", None),
    "Sodexo": (False, "global food services group", None),
    "Starling Bank": (False, "founded 2014, 3,000+ staff", 2014),
    "SumUp": (False, "founded 2012, 3,000+ staff", 2012),
    "Thales": (False, "global defence group", None),
    "Tide": (False, "founded 2015, ~1,800 staff", 2015),
    "Twilio": (False, "listed company (2008)", 2008),
    "Uber": (False, "listed company (2009)", 2009),
    "Wise": (False, "listed company (2011)", 2011),
    "Zopa": (False, "founded 2005", 2005),
}


# --------------------------------------------------------------------------- YC
def fetch_yc(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected yc-oss payload shape at {url}")
    return data


def yc_batch_code(batch: str) -> tuple[str, int | None]:
    """'Winter 2021' -> ('W21', 2021). Spring = X, Fall = F, as YC writes them."""
    m = re.match(r"(Winter|Summer|Spring|Fall)\s+(\d{4})", batch or "")
    if not m:
        return (batch or "?"), None
    season = {"Winter": "W", "Summer": "S", "Spring": "X", "Fall": "F"}[m.group(1)]
    return f"{season}{m.group(2)[2:]}", int(m.group(2))


def yc_tags(c: dict) -> dict:
    code, year = yc_batch_code(c.get("batch", ""))
    staff = c.get("team_size")
    tags: dict = {"source": "yc-oss", "yc_batch": code}
    if isinstance(staff, int):
        tags["team_size"] = staff
    evidence = f"YC {code}" + (f", {staff} staff" if isinstance(staff, int) else "")
    if c.get("status") == "Public":
        tags["startup"] = False
        evidence += ", public company"
    elif year is not None and THIS_YEAR - year > STARTUP_MAX_AGE:
        tags["startup"] = False          # at least as old as its batch - too old
    elif isinstance(staff, int) and staff >= STARTUP_MAX_STAFF:
        tags["startup"] = False
    elif year is not None and isinstance(staff, int):
        tags["startup"] = True
    # else: unknown -> leave the field out
    tags["startup_evidence"] = evidence
    return tags


def yc_priority(c: dict) -> tuple:
    regions = set(c.get("regions") or [])
    eu = {"Europe", "Germany", "France", "Spain", "Sweden", "Switzerland", "Netherlands",
          "Ireland", "Denmark", "Norway", "Finland", "Italy", "Portugal", "Poland", "Belgium",
          "Austria", "Estonia"}
    if "United Kingdom" in regions or "London" in (c.get("all_locations") or ""):
        tier = 0
    elif regions & eu:
        tier = 1
    elif regions & {"Remote", "Fully Remote"}:
        tier = 2
    elif regions & {"United States of America", "America / Canada", "Canada"}:
        tier = 3
    else:
        tier = 4
    return tier, -(c.get("team_size") or 0)


def domain_stem(website: str) -> str:
    host = urlparse(website or "").netloc.lower() or (website or "").lower()
    host = re.sub(r"^www\.", "", host).split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org") and len(parts[-1]) == 2:
        parts = parts[:-2]                       # example.co.uk -> example
    elif len(parts) >= 2:
        parts = parts[:-1]                       # example.ai -> example
    return re.sub(r"[^a-z0-9-]", "", parts[-1]) if parts else ""


def yc_slugs(c: dict) -> list[str]:
    name = c.get("name", "")
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    stem = domain_stem(c.get("website", ""))
    hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    out = [s for s in dict.fromkeys([compact, stem, stem.replace("-", ""), hyphen]) if len(s) >= 2]
    return out[:3]


# --------------------------------------------------------------------------- run
def load_registry() -> tuple[list[dict], set]:
    data = yaml.safe_load(REGISTRY.read_text("utf-8")) if REGISTRY.exists() else {}
    companies = (data or {}).get("companies", []) or []
    seen = {(c.get("ats"), str(c.get("slug", "")).lower()) for c in companies}
    return companies, seen


def run_curated(pool: cf.ThreadPoolExecutor, seen: set, log) -> tuple[list[dict], dict]:
    results, stats = [], {"tried": 0, "verified": 0}
    # Workday: one POST per listed board.
    wd = [w for w in CURATED_WORKDAY if ("workday", w[1].lower()) not in seen]
    stats["tried"] += len({w[0] for w in wd})
    futs = {pool.submit(probe_workday, w[1]): w for w in wd}
    for fut in cf.as_completed(futs):
        company, slug, evidence, market = futs[fut]
        try:
            hit = fut.result()
        except RuntimeError:
            hit = None
        if hit:
            results.append({"company": company, "ats": "workday", "slug": slug,
                            "jobs_seen": hit["count"], "startup": False,
                            "startup_evidence": evidence, "source": "curated",
                            "market": market, "sample_title": hit["title"]})
            log(f"WD   FOUND {company:32} {slug:48} ({hit['count']})")
        else:
            log(f"WD   ----- {company:32} {slug}")
    # Simple ATS: a handful of slug guesses each.
    futs = {pool.submit(find_simple, c[0], c[1], CURATED_ATS_ORDER): c for c in CURATED_SIMPLE}
    stats["tried"] += len(CURATED_SIMPLE)
    for fut in cf.as_completed(futs):
        company, _slugs, market, startup, evidence, founded, staff = futs[fut]
        hit, notes = fut.result()
        if hit and (hit["ats"], hit["slug"].lower()) not in seen:
            entry = {"company": company, **{k: hit[k] for k in ("ats", "slug", "jobs_seen")}}
            if startup is not None:
                entry["startup"] = startup
            if evidence:
                entry["startup_evidence"] = evidence
            if founded:
                entry["founded"] = founded
            if staff:
                entry["team_size"] = staff
            entry.update({"source": "curated", "market": market,
                          "sample_title": hit["sample_title"], "name_check": hit["name_check"]})
            results.append(entry)
            log(f"ATS  FOUND {company:32} {hit['ats']:16} {hit['slug']:24} ({hit['jobs_seen']})")
        else:
            log(f"ATS  ----- {company:32} {'; '.join(notes)}")
    stats["verified"] = len({r["company"] for r in results})
    return results, stats


def run_yc(pool: cf.ThreadPoolExecutor, seen: set, existing_names: set, limit: int,
           log) -> tuple[list[dict], dict]:
    hiring = fetch_yc(YC_HIRING_URL)
    active = [c for c in hiring if c.get("isHiring") and c.get("status") == "Active"]
    active = [c for c in active if norm(c.get("name", "")) not in existing_names]
    active.sort(key=yc_priority)
    chosen = active[:limit]
    stats = {"hiring": len(hiring), "active_hiring": len(active), "probed": 0,
             "verified": 0, "unverified_name": 0, "rejected_mismatch": 0}
    log(f"yc-oss: {len(hiring)} hiring, {len(active)} active & not already registered; "
        f"probing up to {len(chosen)}")

    def one(c: dict):
        stem = domain_stem(c.get("website", ""))
        return c, find_simple(c["name"], yc_slugs(c), YC_ATS_ORDER, extra_terms=[stem])

    results = []
    futs = [pool.submit(one, c) for c in chosen]
    for i, fut in enumerate(cf.as_completed(futs), 1):
        c, (hit, notes) = fut.result()
        if "budget exhausted" in notes:
            continue
        stats["probed"] += 1
        stats["rejected_mismatch"] += sum(1 for n in notes if "belongs to" in n)
        if hit and (hit["ats"], hit["slug"].lower()) not in seen:
            entry = {"company": c["name"], **{k: hit[k] for k in ("ats", "slug", "jobs_seen")}}
            entry.update(yc_tags(c))
            entry.update({"website": c.get("website", ""),
                          "location": (c.get("all_locations") or "")[:120],
                          "sample_title": hit["sample_title"], "name_check": hit["name_check"]})
            results.append(entry)
            if hit["name_check"] == "unverified":
                stats["unverified_name"] += 1
            log(f"[{i:4}/{len(chosen)}] FOUND {c['name']:28} {hit['ats']:10} "
                f"{hit['slug']:22} ({hit['jobs_seen']}) {hit['name_check']}  "
                f"req={BUDGET.used}")
        elif i % 25 == 0:
            log(f"[{i:4}/{len(chosen)}] ... req={BUDGET.used}")
    stats["verified"] = len(results)
    return results, stats


def tag_existing(companies: list[dict], log) -> dict:
    """Tags for registry companies: yc-oss data first, then the curated table."""
    tags: dict[str, dict] = {}
    try:
        yc_all = fetch_yc(YC_ALL_URL)
    except Exception as exc:
        log(f"could not fetch yc-oss all.json: {exc}")
        yc_all = []
    by_name = {norm(c.get("name", "")): c for c in yc_all}
    for name in sorted({c["company"] for c in companies}, key=str.lower):
        yc = by_name.get(norm(name))
        if yc:
            t = yc_tags(yc)
            # A famous name can collide with a small YC company; require the YC entry to
            # be plausibly the same employer (the curated table wins on disagreement).
            if name in EXISTING_TAGS:
                startup, evidence, founded = EXISTING_TAGS[name]
                t = {"startup": startup, "startup_evidence": f"{evidence}; also {t['startup_evidence']}",
                     "source": "curated+yc-oss", "yc_batch": t["yc_batch"],
                     **({"founded": founded} if founded else {}),
                     **({"team_size": t["team_size"]} if "team_size" in t else {})}
            tags[name] = t
        elif name in EXISTING_TAGS:
            startup, evidence, founded = EXISTING_TAGS[name]
            t = {"startup": startup, "startup_evidence": evidence, "source": "curated"}
            if founded:
                t["founded"] = founded
            tags[name] = t
    return tags


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-requests", type=int, default=2500)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--yc-limit", type=int, default=450, help="max YC companies to probe")
    ap.add_argument("--skip-yc", action="store_true")
    ap.add_argument("--skip-curated", action="store_true")
    args = ap.parse_args()
    BUDGET.cap = args.max_requests
    workers = max(1, min(args.workers, 8))
    t0 = time.time()

    def log(msg: str) -> None:
        print(f"{time.time() - t0:6.1f}s {msg}", flush=True)

    companies, seen = load_registry()
    existing_names = {norm(c["company"]) for c in companies}
    extra, stats = [], {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        if not args.skip_curated:
            found, stats["curated"] = run_curated(pool, seen, log)
            extra.extend(found)
            seen |= {(e["ats"], e["slug"].lower()) for e in found}
            existing_names |= {norm(e["company"]) for e in found}
        if not args.skip_yc:
            meta = requests.get(YC_META_URL, headers=HEADERS, timeout=30).json()
            log(f"yc-oss meta last_updated={meta.get('last_updated')}")
            found, stats["yc"] = run_yc(pool, seen, existing_names, args.yc_limit, log)
            extra.extend(found)
    tags = tag_existing(companies, log)

    extra.sort(key=lambda e: (e["company"].lower(), e["ats"]))
    by_ats: dict[str, int] = {}
    for e in extra:
        by_ats[e["ats"]] = by_ats.get(e["ats"], 0) + 1
    stats.update({"requests_used": BUDGET.used, "entries": len(extra), "by_ats": by_ats,
                  "startup_true": sum(1 for e in extra if e.get("startup") is True)})
    header = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "generator": "tools/discover_startups.py",
        "yc_source": YC_HIRING_URL,
        "stats": stats,
    }
    OUT_EXTRA.write_text(yaml.safe_dump({"meta": header, "companies": extra},
                                        sort_keys=False, allow_unicode=True), "utf-8")
    OUT_TAGS.write_text(yaml.safe_dump(
        {"meta": {"generated": header["generated"], "rule":
                  f"startup: true only if founded <= {STARTUP_MAX_AGE}y ago and "
                  f"< {STARTUP_MAX_STAFF} staff, both known; unknown -> omitted"},
         "tags": tags}, sort_keys=False, allow_unicode=True), "utf-8")
    log(f"wrote {len(extra)} entries -> {OUT_EXTRA}")
    log(f"wrote tags for {len(tags)} existing companies -> {OUT_TAGS}")
    log(f"stats: {stats}")


if __name__ == "__main__":
    main()
