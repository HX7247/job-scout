"""Research and industry boards for engineering, computing and science roles.

The general boards bury these among sales and retail jobs; these are the places that
list nothing else. Each was checked live on 2026-10-05.

  * jobs.ac.uk - the UK's academic and research board: research assistants, KTP
    associates, research software engineers, technicians and studentships at
    universities and research institutes. Its search pages are allowed by robots.txt
    and server-rendered, one `j-search-result__result` card per job, filtered by
    discipline facet. Its terms allow personal, non-commercial use.
  * Science Careers (AAAS) and Physics Today Jobs (AIP) - Madgex boards that publish
    an official RSS feed of any keyword search (/jobsrss/?keywords=...). Science
    Careers is labs and research institutes; Physics Today is physics, engineering
    and national-lab roles, mostly in the US. Their feeds ignore the country
    parameter, so each is read once and every posting is placed by its own location.

Nature Careers runs the same platform, but its robots.txt disallows the feed, so it is
not read. New Scientist Jobs, IEEE, ACM and the IET publish no usable feed.

These boards are searched with their own subject keywords rather than the scan's
queries: "industrial placement" finds almost nothing on a research board, while
"research assistant" or "software engineer" finds what it actually lists.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..http import SESSION
from ..models import Job, strip_html
from .aggregators import BaseAggregator
from .customfeeds import _parse_feed

log = logging.getLogger("jobscout.sources.research_boards")

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _day_month(text: str, future: bool, today: date | None = None) -> str | None:
    """"26 Oct" as an ISO date. The year is not printed, so a closing date is taken to
    be the next 26 Oct and a posting date the last one."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", text or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    today = today or date.today()
    try:
        when = date(today.year, _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None
    if future and when < today:
        when = when.replace(year=today.year + 1)
    elif not future and when > today:
        when = when.replace(year=today.year - 1)
    return datetime(when.year, when.month, when.day, tzinfo=timezone.utc).isoformat()


class JobsAcUkSource(BaseAggregator):
    """jobs.ac.uk - UK university and research-institute jobs, by discipline."""

    name = "jobsacuk"
    kind = "board"
    needs_key = False
    uses_query = False
    markets = ("GB",)

    SEARCH = "https://www.jobs.ac.uk/search/"
    PAGE_SIZE = 25
    MAX_PAGES = 8              # per discipline - the newest 200 of each
    # (facet, value) pairs, verified against the live search form.
    DISCIPLINES = (
        ("academicDisciplineFacet[]", "computer-sciences"),
        ("academicDisciplineFacet[]", "engineering-and-technology"),
        ("academicDisciplineFacet[]", "mathematics-and-statistics"),
        ("academicDisciplineFacet[]", "physical-and-environmental-sciences"),
        ("nonAcademicDisciplineFacet[]", "it-services"),
        ("nonAcademicDisciplineFacet[]", "web-design-and-development"),
    )

    def _parse_page(self, html: str, discipline: str) -> list[Job]:
        jobs = []
        for card in BeautifulSoup(html, "lxml").select(".j-search-result__result"):
            text = card.select_one(".j-search-result__text")
            link = text.find("a", href=True) if text else None
            if not link:
                continue
            title = link.get_text(" ", strip=True)
            employer = card.select_one(".j-search-result__employer")
            department = card.select_one(".j-search-result__department")
            fields: dict[str, str] = {}
            for div in text.find_all("div", recursive=False):
                label, sep, value = div.get_text(" ", strip=True).partition(":")
                if sep:
                    fields[label.strip().lower()] = re.sub(r"\s+", " ", value).strip()
            closes = card.select_one(".j-search-result__date--blue")
            salary = fields.get("salary", "")
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=employer.get_text(" ", strip=True) if employer else "",
                title=title, url=urljoin(self.SEARCH, link["href"]),
                external_id=card.get("data-advert-id", ""),
                location=fields.get("location", ""),
                department=department.get_text(" ", strip=True) if department else "",
                description=strip_html(" | ".join(
                    f"{k.title()}: {v}" for k, v in fields.items() if v)),
                salary_raw=salary, salary_currency="GBP" if "£" in salary else "",
                posted_at=_day_month(fields.get("date placed", ""), future=False),
                closes_at=_day_month(closes.get_text(strip=True) if closes else "", future=True),
                raw={"discipline": discipline},
            ))
        return jobs

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        if home and not self.serves(home):
            return []
        per_discipline: list[list[Job]] = []
        for facet, value in self.DISCIPLINES:
            found: list[Job] = []
            for page in range(self.MAX_PAGES):
                html = SESSION.get_text(self.SEARCH, params={
                    facet: value, "pageSize": self.PAGE_SIZE,
                    "startIndex": page * self.PAGE_SIZE + 1}, check_robots=True)
                if not html:
                    break
                got = self._parse_page(html, value)
                found += got
                if len(got) < self.PAGE_SIZE:
                    break
            if not found:
                log.info("jobsacuk: nothing parsed for %s - layout changed?", value)
            per_discipline.append(found)
        # Round-robin so one big discipline does not use the whole budget, and a job
        # listed under two disciplines is kept once.
        jobs, seen, depth = [], set(), 0
        while len(jobs) < max_results and any(depth < len(d) for d in per_discipline):
            for found in per_discipline:
                if depth < len(found) and found[depth].url not in seen:
                    seen.add(found[depth].url)
                    jobs.append(found[depth])
            depth += 1
        return jobs[:max_results]


class MadgexFeedSource(BaseAggregator):
    """A Madgex-hosted board's official RSS search feed (see the module docstring)."""

    kind = "board"
    needs_key = False
    uses_query = False
    host = ""
    label = ""
    PAGES = 2                  # 20 newest per page
    KEYWORDS = ("internship", "intern", "placement", "graduate", "student",
                "research assistant", "software engineer", "machine learning",
                "data scientist", "computer science", "engineer")

    def _job_from(self, item: dict) -> Job | None:
        # Description: "Salary:\n\nEmployer:\nsnippet...\nLocation"
        lines = [ln.strip() for ln in (item.get("description") or "").splitlines() if ln.strip()]
        title = item["title"]
        employer = ""
        if len(lines) >= 3 and lines[1].endswith(":"):
            employer = lines[1].rstrip(":").strip()
        if employer and title.startswith(employer + ":"):
            title = title[len(employer) + 1:].strip()
        elif ": " in title:
            employer, title = (p.strip() for p in title.split(": ", 1))
        if not employer or not title:
            return None
        salary = lines[0].rstrip(":").strip() if lines else ""
        location = lines[-1] if len(lines) >= 3 else ""
        parts = urlsplit(item["url"])
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # drop TrackID/utm
        job_id = re.search(r"/job/(\d+)/", parts.path)
        try:
            posted = parsedate_to_datetime(item.get("published") or "").isoformat()
        except (TypeError, ValueError):
            posted = None
        return Job(
            source=self.name, source_kind=self.kind, company=employer, title=title,
            url=url, external_id=job_id.group(1) if job_id else "",
            location=location, description=strip_html(" ".join(lines[2:-1])),
            salary_raw=salary if re.search(r"\d", salary) else "",
            posted_at=posted, raw={"board": self.label},
        )

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for keyword in self.KEYWORDS:
            for page in range(1, self.PAGES + 1):
                text = SESSION.get_text(f"{self.host}/jobsrss/",
                                        params={"keywords": keyword, "page": page},
                                        check_robots=True)
                items = _parse_feed(text, self.host) if text else []
                for item in items:
                    job = self._job_from(item)
                    if job and job.url not in seen:
                        seen.add(job.url)
                        jobs.append(job)
                if len(items) < 20 or len(jobs) >= max_results:
                    break
            if len(jobs) >= max_results:
                break
        return jobs[:max_results]


class ScienceCareersSource(MadgexFeedSource):
    """Science Careers (AAAS) - research institutes, labs and science industry."""

    name = "sciencecareers"
    label = "Science Careers"
    host = "https://jobs.sciencecareers.org"


class PhysicsTodaySource(MadgexFeedSource):
    """Physics Today Jobs (AIP) - physics, engineering and national-lab roles."""

    name = "physicstoday"
    label = "Physics Today Jobs"
    host = "https://jobs.physicstoday.org"


RESEARCH_BOARD_ADAPTERS = {
    a.name: a() for a in (JobsAcUkSource, ScienceCareersSource, PhysicsTodaySource)
}
