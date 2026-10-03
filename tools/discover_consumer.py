"""Find career boards for retail, hospitality, care and logistics employers.

The existing registry was seeded from tech, finance and engineering names, so the
app could not see a Costa or a Greggs job at all. These employers hire constantly,
in volume, part-time, and are where most students actually earn.

Probes every simple-GET ATS and Workday for each candidate, so one run covers both.

Run:  python tools/discover_consumer.py
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

HEADERS = {"User-Agent": "JobScout/0.1 (personal job-search assistant)"}
TIMEOUT = 10

SIMPLE_PROBES = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs"),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", "jobs"),
    "recruitee": ("https://{slug}.recruitee.com/api/offers/", "offers"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1", "content"),
}
WORKDAY_HOSTS = ["wd1", "wd3", "wd5", "wd2", "wd103"]

# (display name, [slug guesses]) - consumer brands trade under short names.
CANDIDATES: list[tuple[str, list[str]]] = [
    ("Costa Coffee", ["costa", "costacoffee"]),
    ("Greggs", ["greggs"]),
    ("Pret A Manger", ["pret", "pretamanger"]),
    ("Caffe Nero", ["caffenero", "nero"]),
    ("Starbucks", ["starbucks"]),
    ("McDonalds", ["mcdonalds"]),
    ("KFC", ["kfc", "yum"]),
    ("Nandos", ["nandos"]),
    ("Wagamama", ["wagamama"]),
    ("PizzaExpress", ["pizzaexpress"]),
    ("Five Guys", ["fiveguys"]),
    ("Dominos", ["dominos"]),
    ("Greene King", ["greeneking"]),
    ("Mitchells & Butlers", ["mitchellsandbutlers", "mbplc"]),
    ("Whitbread", ["whitbread"]),
    ("Marks and Spencer", ["marksandspencer", "mands"]),
    ("Tesco", ["tesco"]),
    ("Sainsburys", ["sainsburys"]),
    ("Asda", ["asda"]),
    ("Morrisons", ["morrisons"]),
    ("Aldi", ["aldi", "aldiuk"]),
    ("Lidl", ["lidl", "lidluk"]),
    ("John Lewis", ["johnlewis", "jlp"]),
    ("Boots", ["boots", "bootsuk"]),
    ("Superdrug", ["superdrug"]),
    ("JD Sports", ["jdsports", "jdplc"]),
    ("Primark", ["primark"]),
    ("Next", ["next", "nextplc"]),
    ("H&M", ["hm", "hennesmauritz"]),
    ("Uniqlo", ["uniqlo", "fastretailing"]),
    ("TK Maxx", ["tkmaxx", "tjx"]),
    ("B&Q", ["bandq", "kingfisher"]),
    ("Screwfix", ["screwfix"]),
    ("Currys", ["currys"]),
    ("IKEA", ["ikea"]),
    ("Decathlon", ["decathlon"]),
    ("Lush", ["lush"]),
    ("The Body Shop", ["thebodyshop", "bodyshop"]),
    ("B&M", ["bandm", "bmstores"]),
    ("WHSmith", ["whsmith"]),
    ("Waterstones", ["waterstones"]),
    ("Holland & Barrett", ["hollandandbarrett", "hollandbarrett"]),
    ("River Island", ["riverisland"]),
    ("New Look", ["newlook"]),
    ("Deliveroo", ["deliveroo"]),
    ("Just Eat", ["justeattakeaway", "justeat"]),
    ("Uber", ["uber"]),
    ("DPD", ["dpd", "dpduk"]),
    ("Evri", ["evri", "hermes"]),
    ("Royal Mail", ["royalmail"]),
    ("DHL", ["dhl"]),
    ("Compass Group", ["compassgroup", "compass"]),
    ("Sodexo", ["sodexo"]),
    ("ISS", ["issworld", "iss"]),
    ("Mitie", ["mitie"]),
    ("Bupa", ["bupa"]),
    ("Care UK", ["careuk"]),
    ("Barchester Healthcare", ["barchester"]),
    ("HC-One", ["hcone"]),
    ("The Gym Group", ["thegymgroup", "gymgroup"]),
    ("PureGym", ["puregym"]),
    ("David Lloyd", ["davidlloyd"]),
    ("Travis Perkins", ["travisperkins"]),
    ("Pizza Hut", ["pizzahut"]),
    ("Leon", ["leon"]),
    ("Itsu", ["itsu"]),
    ("Gail's", ["gails", "gailsbread"]),
]


def count_from(payload, key: str | None) -> int:
    if payload is None:
        return -1
    if key is None:
        return len(payload) if isinstance(payload, list) else -1
    if isinstance(payload, dict) and key in payload:
        return len(payload[key])
    return -1


def probe_simple(ats: str, slug: str) -> int:
    url_tpl, key = SIMPLE_PROBES[ats]
    try:
        resp = requests.get(url_tpl.format(slug=slug), headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return -1
        return count_from(resp.json(), key)
    except Exception:
        return -1


def probe_workday(tenant: str) -> tuple[str, int] | None:
    cap = tenant.capitalize()
    sites = [f"{cap}Careers", "Careers", "External", f"{cap}_Careers",
             f"{cap}External", "Jobs", f"{cap}Jobs", "careers"]
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    for host in WORKDAY_HOSTS:
        for site in sites:
            url = (f"https://{tenant}.{host}.myworkdayjobs.com"
                   f"/wday/cxs/{tenant}/{site}/jobs")
            try:
                resp = requests.post(url, headers={**HEADERS,
                                                   "Content-Type": "application/json"},
                                     json=body, timeout=TIMEOUT)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                total = int(data.get("total") or len(data.get("jobPostings") or []))
                if total > 0:
                    return f"{tenant}:{host}:{site}", total
            except Exception:
                continue
    return None


def find(entry: tuple[str, list[str]]) -> dict | None:
    company, slugs = entry
    for slug in slugs:
        for ats in SIMPLE_PROBES:
            n = probe_simple(ats, slug)
            if n > 0:
                return {"company": company, "ats": ats, "slug": slug, "jobs_seen": n}
    for slug in slugs:
        hit = probe_workday(slug)
        if hit:
            return {"company": company, "ats": "workday",
                    "slug": hit[0], "jobs_seen": hit[1]}
    return None


def main() -> None:
    found, missing = [], []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(find, c): c for c in CANDIDATES}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            company = futures[fut][0]
            try:
                hit = fut.result()
            except Exception:
                hit = None
            if hit:
                found.append(hit)
                print(f"[{i:3}/{len(CANDIDATES)}] FOUND {company:24} {hit['ats']:16} "
                      f"{hit['slug']:34} ({hit['jobs_seen']})", flush=True)
            else:
                missing.append(company)
                print(f"[{i:3}/{len(CANDIDATES)}] ----- {company}", flush=True)

    registry = ROOT / "data" / "companies_verified.yaml"
    data = yaml.safe_load(registry.read_text("utf-8")) if registry.exists() else {}
    companies = data.get("companies", [])
    known = {(c.get("company"), c.get("ats")) for c in companies}
    added = [f for f in found if (f["company"], f["ats"]) not in known]
    companies.extend(added)
    companies.sort(key=lambda c: c["company"].lower())
    data["companies"] = companies
    data["not_found"] = sorted(set(data.get("not_found", [])) | set(missing))
    registry.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")

    print(f"\nfound {len(found)}/{len(CANDIDATES)}; {len(added)} new -> {registry}")
    print(f"registry now holds {len(companies)} employers")


if __name__ == "__main__":
    main()
