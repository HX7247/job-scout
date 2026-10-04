"""TARGETjobs (targetjobs.co.uk) - the UK's biggest placement-year listing.

Checked 2026-10-04: robots.txt allows everything; the publisher's (GTI) terms forbid
commercial reproduction only, and this is a personal job-search tool. The site's own
search page calls a JSON search service, used here exactly as the page uses it - the
service answers nothing without the Origin/Referer the page sends, so those are set to
the site itself, which is where the request is made from as far as it is concerned.

~650 placements and ~600 internships live at any one time, each with a closing date,
start date and, where the employer gives one, salary - fields the HTML-scraped boards
mostly lack. The query is not used: the type filter is what matters for a placement
student, and the app's own scoring ranks the rest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from ..http import SESSION
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.targetjobs")

SITE = "https://targetjobs.co.uk"
SEARCH = f"{SITE}/ext/svc/inferno-search-service-1-0/search"
HEADERS = {
    "X-Host": "users.targetjobs.co.uk",
    "Origin": SITE,
    "Referer": f"{SITE}/search/jobs",
    "Accept": "application/json",
}
PAGE = 100


def unwrap_link(url: str | None) -> str:
    """The employer's own link out of an ad-tracker or mail-scanner wrapper.

    About a fifth of TARGETjobs' apply links go through ad.doubleclick.net, and a few
    through Outlook safelinks - both carry the real address inside them. A wrapper
    with nothing recoverable inside is dropped rather than kept as the apply link.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    host = urlparse(url).netloc.lower()
    if host.endswith("safelinks.protection.outlook.com"):
        inner = parse_qs(urlparse(url).query).get("url", [""])[0]
        return unwrap_link(unquote(inner)) if inner else ""
    if host.endswith("doubleclick.net"):
        for marker in ("?http", ";http", "=http"):
            at = url.find(marker)
            if at != -1:
                return unwrap_link(unquote(url[at + 1:]))
        return ""
    return url


def _month_year(seconds) -> str:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime("%B %Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _money(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _body(kind: str, offset: int) -> dict:
    return {
        "keys": [""], "groupBy": None,
        "conditionGroup": {"conjunction": "AND", "groups": [
            {"conjunction": "OR", "conditions": [
                {"name": "opportunity_type", "value": kind, "operator": "="}]},
            # still open: deadline not between the epoch and now
            {"conjunction": "OR", "conditions": [
                {"name": "application_deadline_date", "value": ["0", "NOW"],
                 "operator": "NOT BETWEEN"}]},
            {"conjunction": "OR", "conditions": [
                {"name": "type", "value": "opportunity", "operator": "="}]},
        ]},
        # nid breaks ties: on the deadline alone, postings sharing a closing date came
        # back in a different order page to page, and 40% were skipped or repeated.
        "facets": [], "sort": [{"field": "application_deadline_date", "value": "asc"},
                               {"field": "nid", "value": "asc"}],
        "limit": PAGE, "offset": offset, "includePromoted": False,
    }


class TargetJobsSource(BaseAggregator):
    name = "targetjobs"
    kind = "board"
    uses_query = False
    markets = ("GB",)
    KINDS = ("Placement", "Internship")

    def _job(self, doc: dict, kind: str) -> Job | None:
        title = (doc.get("title") or "").strip()
        org = doc.get("organisation") or {}
        # Postings TARGETjobs scraped itself (about 45%) have an empty organisation
        # and name the employer in sourceOrganisationName instead.
        company = ((org.get("title") if isinstance(org, dict) else "")
                   or doc.get("sourceOrganisationName") or "")
        path = doc.get("path") or ""
        if not (title and company and path):
            return None
        # Either {"currency", "lower", "upper"} (numbers as strings) or {"ranges":
        # ["£25,000 to £30,000"]}, which the app's salary parser reads from salary_raw.
        salary = doc.get("salary") if isinstance(doc.get("salary"), dict) else {}
        lower, upper = _money(salary.get("lower")), _money(salary.get("upper"))
        ranges = salary.get("ranges") or []
        salary_raw = "; ".join(str(r) for r in ranges) if isinstance(ranges, list) else ""
        currency = (salary.get("currency") or ("GBP" if salary_raw else "")).upper()
        starts = _month_year(doc.get("opportunityStartDate"))
        location = doc.get("location") or ""
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location if x)
        # A free-text "location" sometimes holds a sentence, not a place.
        if len(location) > 160:
            location = ""
        apply_url = unwrap_link(doc.get("applicationUrl"))
        lead = [f"Opportunity type: {kind}"]
        if starts:
            lead.append(f"Start date: {starts}")
        return Job(
            source=self.name, source_kind=self.kind,
            company=company, title=title,
            url=SITE + path,
            external_id=str(doc.get("nid") or ""),
            location=location,
            description="\n".join(lead) + "\n\n" + strip_html(doc.get("body") or ""),
            salary_raw=salary_raw,
            salary_min=lower,
            salary_max=upper,
            salary_currency=currency if (lower or upper or salary_raw) else "",
            employment_type=kind,
            posted_at=parse_date(doc.get("createdAt")),
            closes_at=parse_date(doc.get("applicationDeadline")),
            raw={"apply_url": apply_url, "start": starts},
        )

    def fetch(self, query: str = "", location: str = "", max_results: int = 1500,
              home=None, **kwargs) -> list[Job]:
        if home and not self.serves(home):
            return []
        jobs: list[Job] = []
        seen: set[str] = set()
        for kind in self.KINDS:
            offset = 0
            while len(jobs) < max_results:
                data = SESSION.post_json(SEARCH, _body(kind, offset), headers=HEADERS)
                search = (data or {}).get("search") or {}
                docs = search.get("documents") or []
                for doc in docs:
                    job = self._job(doc, kind)
                    if job and job.external_id not in seen:
                        seen.add(job.external_id)
                        jobs.append(job)
                offset += PAGE
                if len(docs) < PAGE or offset >= int(search.get("result_count") or 0):
                    break
        return jobs[:max_results]
