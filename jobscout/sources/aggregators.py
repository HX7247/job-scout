"""Third-party job board adapters.

Two tiers:
  * keyed   - Adzuna and Reed, the two with real UK depth (free keys, see README)
  * keyless - open JSON APIs that need no registration at all
"""
from __future__ import annotations

import logging
import os
import re

from ..http import SESSION
from ..models import Job, strip_html, parse_date

log = logging.getLogger("jobscout.sources.aggregators")


class BaseAggregator:
    name = "aggregator"
    kind = "aggregator"
    needs_key = False
    # False for sources that return the same listing whatever the query is - the
    # pipeline then calls them once with the whole budget instead of once per query,
    # which just re-fetched the same first page N times and capped each copy.
    uses_query = True

    def available(self) -> bool:
        return True

    markets: tuple[str, ...] = ()      # empty == serves every market

    def serves(self, home) -> bool:
        return not self.markets or home is None or home.code in self.markets

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        raise NotImplementedError


class AdzunaSource(BaseAggregator):
    """Adzuna - the broadest UK aggregator. Free key at developer.adzuna.com."""

    name = "adzuna"
    needs_key = True

    def available(self) -> bool:
        return bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              country: str = "", max_days_old: int | None = None,
              salary_min: int | None = None, home=None, **kwargs) -> list[Job]:
        if not self.available():
            return []
        country = (country or (home.adzuna if home else "") or "gb").lower()
        if home and not home.adzuna:
            log.info("Adzuna does not cover %s - skipping", home.name)
            return []
        app_id = os.environ["ADZUNA_APP_ID"]
        app_key = os.environ["ADZUNA_APP_KEY"]
        jobs, page = [], 1
        while len(jobs) < max_results and page <= 10:
            params = {
                "app_id": app_id, "app_key": app_key,
                "results_per_page": 50, "what": query,
                "content-type": "application/json",
            }
            if location:
                params["where"] = location
            if max_days_old:
                params["max_days_old"] = max_days_old
            if salary_min:
                params["salary_min"] = salary_min
            data = SESSION.get_json(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                params=params, check_robots=False,
            )
            if not data or not data.get("results"):
                break
            for item in data["results"]:
                loc = item.get("location") or {}
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=(item.get("company") or {}).get("display_name", ""),
                    title=item.get("title", ""),
                    url=item.get("redirect_url", ""),
                    external_id=str(item.get("id", "")),
                    location=loc.get("display_name", ""),
                    description=strip_html(item.get("description", "")),
                    salary_min=item.get("salary_min"),
                    salary_max=item.get("salary_max"),
                    salary_currency=(home.currency if home else ""),
                    employment_type=item.get("contract_time", "") or item.get("contract_type", ""),
                    department=(item.get("category") or {}).get("label", ""),
                    posted_at=parse_date(item.get("created")),
                    raw=item,
                ))
            if len(data["results"]) < 50:
                break
            page += 1
        return jobs[:max_results]


class ReedSource(BaseAggregator):
    """Reed.co.uk - strong UK coverage. Free key at reed.co.uk/developers."""

    name = "reed"
    needs_key = True

    markets = ("GB",)          # Reed is a UK-only service

    def available(self) -> bool:
        return bool(os.environ.get("REED_API_KEY"))

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              distance: int = 15, home=None, **kwargs) -> list[Job]:
        if not self.available():
            return []
        if home and home.code not in self.markets:
            log.info("Reed only covers the UK - skipping for %s", home.name)
            return []
        import base64

        token = base64.b64encode(f"{os.environ['REED_API_KEY']}:".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        jobs, skip = [], 0
        while len(jobs) < max_results and skip < 500:
            params = {"keywords": query, "resultsToTake": 100, "resultsToSkip": skip}
            if location:
                params["locationName"] = location
                params["distanceFromLocation"] = distance
            data = SESSION.get_json("https://www.reed.co.uk/api/1.0/search",
                                    params=params, headers=headers, check_robots=False)
            if not data or not data.get("results"):
                break
            for item in data["results"]:
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=item.get("employerName", ""),
                    title=item.get("jobTitle", ""),
                    url=item.get("jobUrl", ""),
                    external_id=str(item.get("jobId", "")),
                    location=item.get("locationName", ""),
                    description=strip_html(item.get("jobDescription", "")),
                    salary_min=item.get("minimumSalary"),
                    salary_max=item.get("maximumSalary"),
                    salary_currency=item.get("currency", "GBP"),
                    employment_type="contract" if item.get("contractType") else "",
                    posted_at=parse_date(item.get("date")),
                    closes_at=parse_date(item.get("expirationDate")),
                    raw=item,
                ))
            if len(data["results"]) < 100:
                break
            skip += 100
        return jobs[:max_results]


class ArbeitnowSource(BaseAggregator):
    """Open European job board API, no key."""

    name = "arbeitnow"

    uses_query = False

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        jobs, page = [], 1
        while len(jobs) < max_results and page <= 5:
            data = SESSION.get_json("https://www.arbeitnow.com/api/job-board-api",
                                    params={"page": page}, check_robots=False)
            if not data or not data.get("data"):
                break
            for item in data["data"]:
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=item.get("company_name", ""),
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    external_id=str(item.get("slug", "")),
                    location=item.get("location", ""),
                    remote=bool(item.get("remote")),
                    description=strip_html(item.get("description", "")),
                    employment_type=", ".join(item.get("job_types") or []),
                    posted_at=parse_date(item.get("created_at")),
                    raw={"tags": item.get("tags")},
                ))
            page += 1
        return jobs[:max_results]


class RemotiveSource(BaseAggregator):
    name = "remotive"

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        params = {"limit": min(max_results, 200)}
        if query:
            params["search"] = query
        data = SESSION.get_json("https://remotive.com/api/remote-jobs",
                                params=params, check_robots=False)
        if not data or not data.get("jobs"):
            return []
        jobs = []
        for item in data["jobs"][:max_results]:
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=item.get("company_name", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                external_id=str(item.get("id", "")),
                location=item.get("candidate_required_location", ""),
                remote=True,
                description=strip_html(item.get("description", "")),
                salary_raw=item.get("salary", ""),
                employment_type=item.get("job_type", ""),
                department=item.get("category", ""),
                posted_at=parse_date(item.get("publication_date")),
                raw={"tags": item.get("tags")},
            ))
        return jobs


class RemoteOKSource(BaseAggregator):
    name = "remoteok"
    uses_query = False

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        data = SESSION.get_json("https://remoteok.com/api", check_robots=False)
        if not isinstance(data, list):
            return []
        jobs = []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue  # first element is a legal/attribution notice
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=item.get("company", ""),
                title=item.get("position", ""),
                url=item.get("url", "") or item.get("apply_url", ""),
                external_id=str(item.get("id", "")),
                location=item.get("location", "") or "Remote",
                remote=True,
                description=strip_html(item.get("description", "")),
                salary_min=item.get("salary_min") or None,
                salary_max=item.get("salary_max") or None,
                salary_currency="USD",
                posted_at=parse_date(item.get("date") or item.get("epoch")),
                raw={"tags": item.get("tags")},
            ))
            if len(jobs) >= max_results:
                break
        return jobs


class JobicySource(BaseAggregator):
    name = "jobicy"

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        params = {"count": min(max_results, 50)}
        if query:
            params["tag"] = query
        data = SESSION.get_json("https://jobicy.com/api/v2/remote-jobs",
                                params=params, check_robots=False)
        if not data or not data.get("jobs"):
            return []
        jobs = []
        for item in data["jobs"]:
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=item.get("companyName", ""),
                title=item.get("jobTitle", ""),
                url=item.get("url", ""),
                external_id=str(item.get("id", "")),
                location=item.get("jobGeo", ""),
                remote=True,
                description=strip_html(item.get("jobExcerpt") or item.get("jobDescription", "")),
                salary_min=item.get("annualSalaryMin") or None,
                salary_max=item.get("annualSalaryMax") or None,
                salary_currency=item.get("salaryCurrency", ""),
                employment_type=", ".join(item.get("jobType") or []),
                department=", ".join(item.get("jobIndustry") or []),
                posted_at=parse_date(item.get("pubDate")),
                raw=item,
            ))
        return jobs


class HimalayasSource(BaseAggregator):
    name = "himalayas"
    uses_query = False

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        data = SESSION.get_json("https://himalayas.app/jobs/api",
                                params={"limit": min(max_results, 100)}, check_robots=False)
        if not data or not data.get("jobs"):
            return []
        jobs = []
        for item in data["jobs"]:
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=item.get("companyName", ""),
                title=item.get("title", ""),
                url=item.get("applicationLink", "") or item.get("guid", ""),
                external_id=str(item.get("guid", "")),
                location=", ".join(item.get("locationRestrictions") or []) or "Remote",
                remote=True,
                description=strip_html(item.get("description", "")),
                salary_min=item.get("minSalary") or None,
                salary_max=item.get("maxSalary") or None,
                salary_currency="USD",
                employment_type=item.get("employmentType", ""),
                posted_at=parse_date(item.get("pubDate")),
                raw={"categories": item.get("categories")},
            ))
        return jobs


class TheMuseSource(BaseAggregator):
    name = "themuse"
    uses_query = False

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        jobs, page = [], 0
        while len(jobs) < max_results and page < 5:
            params = {"page": page}
            if location:
                params["location"] = location
            data = SESSION.get_json("https://www.themuse.com/api/public/jobs",
                                    params=params, check_robots=False)
            if not data or not data.get("results"):
                break
            for item in data["results"]:
                locs = ", ".join(l.get("name", "") for l in (item.get("locations") or []))
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=(item.get("company") or {}).get("name", ""),
                    title=item.get("name", ""),
                    url=(item.get("refs") or {}).get("landing_page", ""),
                    external_id=str(item.get("id", "")),
                    location=locs,
                    remote="remote" in locs.lower(),
                    description=strip_html(item.get("contents", "")),
                    department=", ".join(c.get("name", "") for c in (item.get("categories") or [])),
                    posted_at=parse_date(item.get("publication_date")),
                    raw={"levels": [l.get("name") for l in (item.get("levels") or [])]},
                ))
            page += 1
        return jobs[:max_results]


class HNHiringSource(BaseAggregator):
    """Hacker News 'Who is hiring' threads, via the public Algolia search API."""

    name = "hn_hiring"
    kind = "board"

    _EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        threads = SESSION.get_json(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "Ask HN: Who is hiring?", "tags": "story",
                    "hitsPerPage": 3, "restrictSearchableAttributes": "title"},
            check_robots=False,
        )
        if not threads or not threads.get("hits"):
            return []
        jobs = []
        for thread in threads["hits"][:2]:
            story_id = thread.get("objectID")
            comments = SESSION.get_json(
                "https://hn.algolia.com/api/v1/search",
                params={"tags": f"comment,story_{story_id}", "hitsPerPage": 200,
                        "query": query or ""},
                check_robots=False,
            )
            if not comments:
                continue
            for hit in comments.get("hits", []):
                text = strip_html(hit.get("comment_text", ""))
                if len(text) < 60:
                    continue
                headline = text.split("\n")[0][:180]
                company = headline.split("|")[0].strip()[:80] or "Unknown"
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=company,
                    title=headline,
                    url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    external_id=str(hit.get("objectID")),
                    location="See posting",
                    description=text,
                    posted_at=parse_date(hit.get("created_at")),
                    raw={"emails": self._EMAIL.findall(text)[:3],
                         "thread": thread.get("title")},
                ))
                if len(jobs) >= max_results:
                    return jobs
        return jobs


AGGREGATOR_ADAPTERS = {
    a.name: a() for a in [
        AdzunaSource, ReedSource, ArbeitnowSource, RemotiveSource, RemoteOKSource,
        JobicySource, HimalayasSource, TheMuseSource, HNHiringSource,
    ]
}
