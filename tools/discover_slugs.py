"""Probe candidate employers across every simple-GET ATS and record which slug works.

Output: data/companies_verified.yaml - the registry the scraper reads.
Run occasionally; company boards migrate between ATS providers.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADERS = {"User-Agent": "JobScout/0.1 (personal job-search assistant)"}
TIMEOUT = 10

PROBES = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs"),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", "jobs"),
    "recruitee": ("https://{slug}.recruitee.com/api/offers/", "offers"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1", "content"),
}

# Employers relevant to a London-based engineering / tech / quant search.
CANDIDATES = [
    # UK fintech & tech
    "Monzo", "Starling Bank", "Revolut", "Wise", "Deliveroo", "Checkout.com", "GoCardless",
    "Octopus Energy", "Darktrace", "Snyk", "Improbable", "Graphcore", "Quantexa", "Tractable",
    "Faculty", "Onfido", "Thought Machine", "Form3", "Zopa", "Moneybox", "Freetrade", "Cleo",
    "Curve", "Tide", "Marshmallow", "Lendable", "Primer", "Paddle", "SumUp", "Soldo", "Modulr",
    "TrueLayer", "Yapily", "ClearScore", "Zilch", "Griffin", "Fnality", "Wayve", "Synthesia",
    "ElevenLabs", "Stability AI", "PolyAI", "Builder.ai", "Peak", "Matillion", "Cognism",
    "Beauhurst", "Multiverse", "Hopin", "Pleo", "Payhawk", "Nutmeg", "PensionBee", "Habito",
    # US tech with London offices
    "Palantir", "Stripe", "Datadog", "Cloudflare", "Databricks", "Figma", "Notion", "Ramp",
    "Anthropic", "OpenAI", "Scale AI", "Discord", "Reddit", "Robinhood", "Coinbase", "Plaid",
    "Airtable", "Vercel", "Linear", "Retool", "Rippling", "Brex", "Gusto", "Asana", "Twilio",
    "MongoDB", "Elastic", "HashiCorp", "GitLab", "Sentry", "PagerDuty", "Miro", "Canva",
    # Trading / quant - relevant to the user's other work
    "Jane Street", "Optiver", "IMC Trading", "Flow Traders", "DRW", "Jump Trading", "Citadel",
    "Two Sigma", "Man Group", "G-Research", "Marshall Wace", "Squarepoint", "XTX Markets",
    "Qube Research", "Millennium", "Balyasny", "Point72", "Susquehanna", "Old Mission",
    # Engineering / industrial / consultancy
    "Arup", "Atkins", "Jacobs", "Mott MacDonald", "WSP", "Ramboll", "AECOM", "Buro Happold",
    "Balfour Beatty", "Laing O'Rourke", "Costain", "Skanska", "Dyson", "Siemens", "Bosch",
    "ABB", "Schneider Electric", "Rolls-Royce", "BAE Systems", "Airbus", "Leonardo", "Thales",
    "QinetiQ", "Babcock", "MBDA", "National Grid", "SSE", "EDF", "Centrica", "Shell", "BP",
    # Pharma / life sciences
    "GSK", "AstraZeneca", "Unilever", "Reckitt", "Haleon", "Moderna", "BenevolentAI",
    # Consultancies / other grad-heavy
    "Accenture", "Capgemini", "Deloitte", "KPMG", "PwC", "EY", "McKinsey", "BCG", "Bain",
]


def slug_variants(name: str) -> list[str]:
    """Plausible board slugs for a company display name."""
    base = name.lower().strip()
    compact = re.sub(r"[^a-z0-9]", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    nosuffix = re.sub(r"\.(com|ai|io|co|uk)$", "", compact)
    pascal = "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", name) if w)
    return list(dict.fromkeys([compact, hyphen, nosuffix, pascal, name.replace(" ", "")]))


def count_from(payload, key: str | None) -> int:
    if payload is None:
        return -1
    if key is None:
        return len(payload) if isinstance(payload, list) else -1
    if isinstance(payload, dict) and key in payload:
        return len(payload[key])
    return -1


def probe(ats: str, slug: str) -> int:
    url_tpl, key = PROBES[ats]
    try:
        resp = requests.get(url_tpl.format(slug=slug), headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return -1
        return count_from(resp.json(), key)
    except Exception:
        return -1


def find_board(company: str) -> dict | None:
    for slug in slug_variants(company):
        for ats in PROBES:
            n = probe(ats, slug)
            if n > 0:
                return {"company": company, "ats": ats, "slug": slug, "jobs_seen": n}
    return None


def main() -> None:
    results, misses = [], []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(find_board, c): c for c in CANDIDATES}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            company = futures[fut]
            try:
                hit = fut.result()
            except Exception:
                hit = None
            if hit:
                results.append(hit)
                print(f"[{i:3}/{len(CANDIDATES)}] FOUND {company:22} {hit['ats']:16} "
                      f"{hit['slug']:22} ({hit['jobs_seen']} jobs)", flush=True)
            else:
                misses.append(company)
                print(f"[{i:3}/{len(CANDIDATES)}] ----- {company}", flush=True)

    results.sort(key=lambda r: r["company"].lower())
    out = ROOT / "data" / "companies_verified.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump({"companies": results, "not_found": sorted(misses)},
                                  sort_keys=False, allow_unicode=True), "utf-8")
    print(f"\nverified {len(results)}/{len(CANDIDATES)} -> {out}")
    print(json.dumps({r["ats"]: sum(1 for x in results if x["ats"] == r["ats"])
                      for r in results}, indent=2))


if __name__ == "__main__":
    main()
