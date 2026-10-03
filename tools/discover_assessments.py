"""Build the employer -> practice-pack index from AssessmentDay's public sitemap.

AssessmentDay publishes a practice page per employer per test type - "aldi-verbal",
"jpmorgan-numerical", "nhs-situational-strengths-test". That is the single most useful
thing an applicant can be handed: practice in the same format the employer actually
sends. Their sitemap lists every one, so this reads the sitemap rather than crawling the
site, and writes a lookup table the app uses offline.

Run:  python tools/discover_assessments.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

SITEMAP = "https://www.assessmentday.co.uk/sitemap.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobScout/0.1; personal job search)"}

PROFILE = re.compile(r"/profiles/([a-z0-9\-]+)\.html?$", re.I)

# Trailing test-type token -> what to call it. Longest first so "situational-strengths"
# is not read as "strengths".
TEST_TYPES = [
    ("situational-strengths-test", "situational strengths test"),
    ("situational-judgement", "situational judgement test"),
    ("critical-thinking", "critical thinking test"),
    ("inductive-reasoning", "inductive reasoning test"),
    ("logical-reasoning", "logical reasoning test"),
    ("diagrammatic", "diagrammatic reasoning test"),
    ("watson-glaser", "Watson Glaser test"),
    ("assessment-centre", "assessment centre"),
    ("video-interview", "video interview"),
    ("numerical", "numerical reasoning test"),
    ("verbal", "verbal reasoning test"),
    ("mechanical", "mechanical reasoning test"),
    ("abstract", "abstract reasoning test"),
    ("personality", "personality questionnaire"),
    ("aptitude", "aptitude tests"),
    ("strengths", "strengths assessment"),
    ("sjt", "situational judgement test"),
    ("online-assessment", "online assessment"),
    ("e-tray", "e-tray exercise"),
    ("logical", "logical reasoning test"),
    ("inductive", "inductive reasoning test"),
    ("spatial", "spatial reasoning test"),
    ("error-checking", "error checking test"),
    ("test", "practice test"),
]

# Slugs that need a proper company name; the rest are title-cased from the slug.
KNOWN_NAMES = {
    "jpmorgan": "JPMorgan", "bnp-paribas": "BNP Paribas", "rbs": "RBS",
    "pwc": "PwC", "ey": "EY", "kpmg": "KPMG", "hsbc": "HSBC", "nhs": "NHS",
    "bbc": "BBC", "ibm": "IBM", "gsk": "GSK", "bp": "BP", "eon": "E.ON",
    "frs": "Civil Service Fast Stream", "lloyds": "Lloyds Banking Group",
    "rolls-royce": "Rolls-Royce", "pa-consulting": "PA Consulting",
    "mi5": "MI5", "mi6": "MI6", "gchq": "GCHQ", "aldi": "Aldi", "lidl": "Lidl",
    "tui": "TUI", "dhl": "DHL", "shell": "Shell", "bt": "BT",
}


def split_slug(slug: str) -> tuple[str, str]:
    """Separate the employer from the test type in a profile slug."""
    for token, label in TEST_TYPES:
        if slug.endswith("-" + token):
            return slug[: -len(token) - 1], label
        if slug == token:
            return "", label
    return slug, "practice test"


def company_name(slug: str) -> str:
    if slug in KNOWN_NAMES:
        return KNOWN_NAMES[slug]
    # "of"/"and" stay lower case; short words that are acronyms go upper. Without the
    # first rule this produced "Bank OF England".
    lower = {"of", "and", "the", "for", "de"}
    words = slug.split("-")
    return " ".join(
        w if w in lower and i else w.upper() if len(w) <= 3 and w.isalpha() else w.title()
        for i, w in enumerate(words))


def main() -> None:
    response = requests.get(SITEMAP, headers=HEADERS, timeout=30)
    response.raise_for_status()
    urls = re.findall(r"<loc>([^<]+)</loc>", response.text)
    print(f"sitemap listed {len(urls)} URLs")

    index: dict[str, dict] = {}
    for url in urls:
        match = PROFILE.search(url)
        if not match:
            continue
        employer_slug, test = split_slug(match.group(1).lower())
        if not employer_slug:
            continue
        entry = index.setdefault(employer_slug, {
            "company": company_name(employer_slug), "tests": []})
        entry["tests"].append({"test": test, "url": url})

    for entry in index.values():
        entry["tests"].sort(key=lambda t: t["test"])

    out = ROOT / "data" / "assessment_profiles.yaml"
    out.write_text(yaml.safe_dump(
        {"source": SITEMAP,
         "note": "Employer-specific practice packs published by AssessmentDay. Linked, "
                 "never copied - the questions stay on their site.",
         "employers": dict(sorted(index.items()))},
        sort_keys=False, allow_unicode=True), "utf-8")

    total = sum(len(e["tests"]) for e in index.values())
    print(f"{len(index)} employers, {total} practice packs -> {out}")
    for slug, entry in sorted(index.items())[:12]:
        print(f"  {entry['company']:28} {', '.join(t['test'] for t in entry['tests'])}")


if __name__ == "__main__":
    main()
