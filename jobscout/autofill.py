"""Fill in a tracked job's details from its own posting, one click per job.

A row on the tracker often has little more than a company and a link - typed in by
hand, imported from a spreadsheet, or scanned from a list (Workday listings carry no
description or closing date at all). This reads the posting behind the link and fills
whatever is still blank: role, location, deadline, salary, start date, duration and the
description. It never overwrites a value that is already there, so nothing the user
typed is ever lost.

Where the details come from, best first (checked against live pages on 2026-10-04):

  Workday     the page is an empty app shell; the same JSON endpoint the shell calls
              (/wday/cxs/...) has the title, location and the posting's end date.
  Greenhouse  the public job-board API has the deadline when the employer sets one.
  most others schema.org JobPosting data embedded in the page for search engines
              (Lever, Ashby, iCIMS, Higherin/RateMyPlacement and many careers sites).
  Gradcracker labelled fields on the page: Deadline / Starting / Duration / Salary.
  any page    the description text: "applications close 31 October", "12-month
              placement", "September 2027 start".

Sites whose terms forbid automated access (LinkedIn, Indeed, Glassdoor, Handshake)
are not fetched at all; robots.txt is obeyed for everything else via the shared
PoliteSession.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import geo
from .http import SESSION
from .models import clean, strip_html
from .tracker_import import parse_day

log = logging.getLogger("jobscout.autofill")

BLOCKED = {
    "linkedin.com": "LinkedIn",
    "indeed.": "Indeed",
    "glassdoor.": "Glassdoor",
    "joinhandshake.com": "Handshake",
    "handshake.com": "Handshake",
}

_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DAY = (rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\.?,?(?:\s+\d{{4}})?"
        rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?!\d)(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?"
        r"|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}")
_MONTH_YEAR = rf"(?:{_MONTHS})\.?\s+\d{{4}}"

_DEADLINE = re.compile(
    r"\b(?:application\s+deadline|closing\s+date|deadline(?:\s+for\s+applications)?|"
    r"applications?\s+(?:will\s+)?close[sd]?|apply\s+(?:by|before)|closes)"
    rf"\s*(?:is|on|:|-|–)?\s*(?:on\s+)?(?:\w+day,?\s+)?({_DAY})", re.I)
_START = re.compile(
    r"\b(?:start(?:ing)?\s+date|starting|start(?:s)?|commenc\w+)\s*(?:is|:|-|–)?\s*"
    rf"(?:in|on|from)?\s*(?:\w+day,?\s+)?({_MONTH_YEAR}|{_DAY})", re.I)
_START_AFTER = re.compile(rf"\b({_MONTH_YEAR})\s+(?:start|intake)\b", re.I)
_NUMBER = r"\d{1,2}(?:\.\d)?|one|two|three|four|five|six|eight|nine|ten|eleven|twelve|fifteen"
_DURATION = re.compile(
    rf"\b(?:duration\s*:?\s*({_NUMBER})[\s-]*(week|month|year)s?"
    rf"|({_NUMBER})[\s-]*(week|month|year)s?(?:\s+long)?\s+"
    r"(?:paid\s+)?(?:industrial\s+|summer\s+|work\s+|sandwich\s+)?"
    r"(?:placement|internship|programme|program|scheme|contract|insight))", re.I)

# Labels a page might show next to a value, as on Gradcracker's job pages.
_LABELS = {
    "deadline": "deadline", "closing date": "deadline", "application deadline": "deadline",
    "applications close": "deadline", "starting": "start", "start date": "start",
    "duration": "duration", "salary": "salary", "location": "location",
}


def blocked_site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return next((name for key, name in BLOCKED.items() if key in host), "")


def _iso(value) -> str:
    """YYYY-MM-DD from an ISO timestamp or a written date, else ''."""
    text = clean(str(value or ""))
    m = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else parse_day(text)


def _start(value: str) -> str:
    """A start date as a day when one is given, otherwise as written ("June 2027")."""
    text = clean(value)
    if re.fullmatch(_MONTH_YEAR, text, re.I):
        return text.title()
    return _iso(text) or text


def _salary(text: str) -> str:
    low, high, currency = geo.parse_salary(text)
    return geo.format_salary(low, high, currency) if low or high else ""


def _duration(match: re.Match) -> str:
    number = match.group(1) or match.group(3)
    unit = (match.group(2) or match.group(4)).lower()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15}
    n = float(number) if number[0].isdigit() else words[number.lower()]
    if unit == "month" and n == 12:
        return "1 Year"
    return f"{n:g} {unit.title()}{'s' if n != 1 else ''}"


def mine_text(text: str) -> dict:
    """Deadline, start, duration and pay mentioned in free text."""
    out = {}
    text = " ".join((text or "").split())
    if m := _DEADLINE.search(text):
        out["deadline"] = _iso(m.group(1))
    if m := _START.search(text) or _START_AFTER.search(text):
        out["start"] = _start(m.group(1))
    if m := _DURATION.search(text):
        out["duration"] = _duration(m)
    if pay := _salary(text):
        out["salary"] = pay
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ page readers
def _job_posting(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (TypeError, ValueError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            kind = item.get("@type") if isinstance(item, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return item
    return {}


def _place(location) -> str:
    places = location if isinstance(location, list) else [location]
    names = []
    for place in places:
        address = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            parts = [address.get("addressLocality"), address.get("addressRegion")]
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            name = ", ".join(clean(str(p)) for p in parts + [country] if p)
        else:
            name = clean(str(address or ""))
        if name and name not in names:
            names.append(name)
    return "; ".join(names[:3])


def _from_job_posting(item: dict) -> dict:
    out = {"title": clean(item.get("title", "")),
           "location": _place(item.get("jobLocation")),
           "deadline": _iso(item.get("validThrough")),
           "posted": _iso(item.get("datePosted")),
           "description": strip_html(item.get("description", ""))}
    pay = item.get("baseSalary")
    if isinstance(pay, dict):
        value = pay.get("value") or {}
        if isinstance(value, dict):
            low, high = value.get("minValue") or value.get("value"), value.get("maxValue")
        else:
            low, high = value, None
        unit = str((value.get("unitText") if isinstance(value, dict) else "") or "").lower()
        currency = pay.get("currency") or ""
        try:
            low, high = (float(low) if low else None), (float(high) if high else None)
        except (TypeError, ValueError):
            low = high = None
        shown = geo.format_salary(low, high, currency)
        if shown and unit in ("hour", "day", "week", "month"):
            # format_salary rounds to whole units, which turns $3.50 an hour into $4.
            prefix = geo.format_salary(1, None, currency)[:-1]
            amounts = [f"{prefix}{v:,.2f}".removesuffix(".00") for v in (low, high) if v]
            shown = f"{' - '.join(dict.fromkeys(amounts))} per {unit}"
        if shown:
            out["salary"] = shown
    return out


def _labelled(soup: BeautifulSoup) -> dict:
    """Values shown under a label: <div>Deadline</div><div>4 December 2026</div>."""
    out = {}
    for node in soup.find_all(string=True):
        key = _LABELS.get(clean(node).rstrip(":").lower())
        if not key or key in out or node.parent is None:
            continue
        label = node.parent
        sibling = label.find_next_sibling()
        if sibling is not None:
            value = sibling.get_text(" ", strip=True)
        else:
            holder = label.parent
            whole = holder.get_text(" ", strip=True) if holder else ""
            value = whole[len(clean(node)):].strip(" :") if whole.startswith(clean(node)) else ""
        value = clean(value)[:120]
        if not value:
            continue
        if key == "deadline":
            value = _iso(value)
        elif key == "start":
            value = _start(value)
        elif key == "duration":
            m = _DURATION.search(f"duration {value}")
            value = _duration(m) if m else ""
        elif key == "salary":
            value = _salary(value) or (value if re.search(r"\d", value) else "")
        if value:
            out[key] = value
    return out


def _workday(url: str):
    m = re.match(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                 r"([^/?#]+)/(job/[^?#]+)", url)
    if not m:
        return None
    tenant, wd, site, rest = m.groups()
    data = SESSION.get_json(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/"
                            f"{tenant}/{site}/{rest}", retries=2, timeout=20)
    info = (data or {}).get("jobPostingInfo")
    if not info:
        return {}
    places = [info.get("location")] + list(info.get("additionalLocations") or [])
    return {"title": clean(info.get("title", "")),
            "location": "; ".join(p for p in places if p)[:200],
            "deadline": _iso(info.get("endDate")),
            "posted": _iso(info.get("startDate")),
            "description": strip_html(info.get("jobDescription", ""))}


def _greenhouse(url: str):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url)
    if not m:
        return None
    data = SESSION.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}",
        retries=2, timeout=20)
    if not data:
        return {}
    return {"title": clean(data.get("title", "")),
            "location": clean((data.get("location") or {}).get("name", "")),
            "deadline": _iso(data.get("application_deadline")),
            "posted": _iso(data.get("first_published")),
            "description": strip_html(data.get("content", ""))}


def _page(url: str):
    html = SESSION.get_text(url, retries=2, timeout=20)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    found = {}
    posting = _job_posting(soup)
    if posting:
        found = _from_job_posting(posting)
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for key, value in _labelled(soup).items():
        if not found.get(key):
            found[key] = value
    if not found.get("title"):
        og = soup.find("meta", property="og:title")
        found["title"] = clean(og["content"]) if og and og.get("content") else ""
    if not found.get("description"):
        main = soup.find("main") or soup.find("article") or soup.body
        found["description"] = clean(main.get_text(" ", strip=True))[:20000] if main else ""
        found["_page_text"] = True          # whole-page text: mine it, do not store it
    return found


def fetch_details(url: str) -> tuple[dict, str]:
    """(details, problem). Details hold whatever the posting gives; problem says why
    nothing could be read, in words for the user."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return {}, "No link to read."
    site = blocked_site(url)
    if site:
        return {}, f"{site} does not allow automated reading - fill this one in by hand."
    try:
        for reader in (_workday, _greenhouse):
            found = reader(url)
            if found is not None:
                break
        else:
            found = _page(url)
    except Exception as exc:                       # a malformed page must not 500 the row
        log.warning("auto-fill failed for %s: %s", url, exc)
        return {}, "Could not read that page."
    if not found:
        return {}, ("The posting could not be reached - it may have been taken down, "
                    "or the site does not allow automated reading.")
    mined = mine_text(found.get("description", ""))
    for key, value in mined.items():
        if not found.get(key):
            found[key] = value
    if found.pop("_page_text", False):
        found["description"] = ""
    return {k: v for k, v in found.items() if v}, ""


# ------------------------------------------------------------------ apply to a job
FIELD_LABELS = {"title": "Role", "location": "Location", "deadline": "Deadline",
                "salary": "Salary", "start": "Start date", "duration": "Duration",
                "posted": "Opened", "description": "Description"}


def _tidy_title(title: str, company: str) -> str:
    """A page title is often "Role | Employer | Site" or "Role - Employer"."""
    title = title.split(" | ")[0].strip()
    if company:
        title = re.sub(rf"\s+(?:-|–|@|at)\s+{re.escape(company)}\s*$", "", title, flags=re.I)
    return title[:200]


def autofill(store, job_id: str) -> dict:
    """Fill the blanks on one job from its posting. Returns what was filled."""
    job = store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    found, problem = fetch_details(job.get("url", ""))
    if not found:
        # A scanned posting may still have a description from the scan to mine.
        found = mine_text(job.get("description", ""))
        if not found:
            return {"filled": {}, "message": problem or "Nothing new found on the posting."}

    filled, columns = {}, {}
    if found.get("title") and job.get("title", "") in ("", "(no role name)"):
        columns["title"] = filled["title"] = _tidy_title(found["title"], job.get("company", ""))
    if found.get("location") and not job.get("location"):
        columns["location"] = filled["location"] = found["location"]
    if found.get("deadline") and not job.get("closes_at"):
        columns["closes_at"] = filled["deadline"] = found["deadline"]
    if found.get("salary") and not job.get("salary_display"):
        columns["salary_display"] = filled["salary"] = found["salary"]
    if found.get("posted") and not job.get("posted_at"):
        columns["posted_at"] = filled["posted"] = found["posted"]
    if found.get("description") and len(job.get("description") or "") < 200:
        columns["description"] = found["description"][:20000]
        filled["description"] = f"{len(columns['description']):,} characters"
    if columns:
        sets = ", ".join(f"{col} = ?" for col in columns)
        store.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*columns.values(), job_id))
        store.conn.commit()

    extra = dict(job.get("tracker_extra") or {})
    for key, column in (("start", "Start Date"), ("duration", "Duration")):
        if found.get(key) and not extra.get(column):
            extra[column] = filled[key] = found[key]
    if any(k in filled for k in ("start", "duration")):
        store.set_tracker_extra(job_id, extra)

    if filled:
        names = ", ".join(FIELD_LABELS[k] for k in filled)
        message = f"Filled {names}."
    else:
        message = "Already complete - nothing blank that the posting could fill."
    return {"filled": filled, "message": message}
