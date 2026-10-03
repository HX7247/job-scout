"""Find Workday career sites for large employers.

Workday is where the big engineering, pharma and infrastructure firms live - exactly
the ones missing from the simple-GET ATS registry. A Workday board is addressed by
three parts, none of which are guessable from the company name alone:

    https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

so this probes plausible combinations and keeps whatever answers with jobs. Verified
entries are appended to data/companies_verified.yaml in the slug form the WorkdaySource
adapter expects: "tenant:wdN:Site".

Run:  python tools/discover_workday.py
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADERS = {"User-Agent": "JobScout/0.1 (personal job-search assistant)",
           "Content-Type": "application/json"}
TIMEOUT = 12
BODY = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

# Workday numbers its data centres; these five cover almost everything.
HOSTS = ["wd1", "wd3", "wd5", "wd2", "wd103"]

# Site names employers actually use, in rough order of frequency.
def site_candidates(tenant: str) -> list[str]:
    cap = tenant.capitalize()
    return [f"{cap}Careers", "Careers", "External", f"{cap}_Careers",
            f"{cap}External", "ExternalCareers", "Jobs", f"{cap}Jobs",
            f"{cap}careers", "careers", f"{tenant}careers"]


# (display name, workday tenant) - tenants are the subdomain, not the company name.
CANDIDATES = [
    ("GSK", "gsk"), ("AstraZeneca", "astrazeneca"), ("Rolls-Royce", "rollsroyce"),
    ("BAE Systems", "baesystems"), ("Airbus", "airbus"), ("Siemens", "siemens"),
    ("Unilever", "unilever"), ("Reckitt", "reckitt"), ("Haleon", "haleon"),
    ("National Grid", "nationalgrid"), ("SSE", "sse"), ("Centrica", "centrica"),
    ("Shell", "shell"), ("BP", "bp"), ("Jacobs", "jacobs"), ("AECOM", "aecom"),
    ("WSP", "wsp"), ("Arup", "arup"), ("Atkins", "atkinsrealis"),
    ("Mott MacDonald", "mottmac"), ("Balfour Beatty", "balfourbeatty"),
    ("Skanska", "skanska"), ("Costain", "costain"), ("Babcock", "babcock"),
    ("QinetiQ", "qinetiq"), ("Thales", "thales"), ("Leonardo", "leonardo"),
    ("Network Rail", "networkrail"), ("Dyson", "dyson"), ("JLR", "jlr"),
    ("Schneider Electric", "schneiderelectric"), ("ABB", "abb"),
    ("Johnson Matthey", "matthey"), ("Croda", "croda"), ("Diageo", "diageo"),
    ("Vodafone", "vodafone"), ("BT", "bt"), ("Barclays", "barclays"),
    ("HSBC", "hsbc"), ("NatWest", "natwest"), ("Lloyds Banking Group", "lloydsbanking"),
    ("Aviva", "aviva"), ("Legal & General", "landg"), ("Tesco", "tesco"),
    ("Sainsburys", "sainsburys"), ("Rolls Royce SMR", "rrsmr"),
    ("Accenture", "accenture"), ("Capgemini", "capgemini"), ("IBM", "ibm"),
    ("Salesforce", "salesforce"), ("Workday", "workday"), ("Nvidia", "nvidia"),
    ("Dell", "dell"), ("Cisco", "cisco"),
]


def probe(tenant: str, host: str, site: str) -> int:
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        resp = requests.post(url, headers=HEADERS, json=BODY, timeout=TIMEOUT)
        if resp.status_code != 200:
            return -1
        data = resp.json()
        return int(data.get("total") or len(data.get("jobPostings") or []))
    except Exception:
        return -1


def find(entry: tuple[str, str]) -> dict | None:
    company, tenant = entry
    for host in HOSTS:
        for site in site_candidates(tenant):
            count = probe(tenant, host, site)
            if count > 0:
                return {"company": company, "ats": "workday",
                        "slug": f"{tenant}:{host}:{site}", "jobs_seen": count}
    return None


def main() -> None:
    found, missing = [], []
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(find, c): c for c in CANDIDATES}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            company = futures[fut][0]
            try:
                hit = fut.result()
            except Exception:
                hit = None
            if hit:
                found.append(hit)
                print(f"[{i:3}/{len(CANDIDATES)}] FOUND {company:22} "
                      f"{hit['slug']:44} ({hit['jobs_seen']} jobs)", flush=True)
            else:
                missing.append(company)
                print(f"[{i:3}/{len(CANDIDATES)}] ----- {company}", flush=True)

    registry = ROOT / "data" / "companies_verified.yaml"
    data = yaml.safe_load(registry.read_text("utf-8")) if registry.exists() else {}
    companies = data.get("companies", [])
    existing = {(c.get("company"), c.get("ats")) for c in companies}
    added = [f for f in found if (f["company"], "workday") not in existing]
    companies.extend(added)
    companies.sort(key=lambda c: c["company"].lower())
    data["companies"] = companies
    data["not_found"] = sorted(set(data.get("not_found", [])) | set(missing))
    registry.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")

    print(f"\nworkday boards found: {len(found)}/{len(CANDIDATES)}; "
          f"{len(added)} new entries written to {registry}")


if __name__ == "__main__":
    main()
