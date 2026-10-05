"""Google Jobs, via SerpApi.

Google publishes no jobs API for job seekers (Cloud Talent Solution is for employers'
own listings) and its terms forbid scraping search results, so this never touches
google.com. SerpApi (serpapi.com) is a commercial API that returns Google Jobs results
as JSON; its own terms and legal position cover how it obtains them. Checked live on
2026-10-01: the documented endpoint answers a bad key with a structured 401.

Google Jobs pools listings from many boards - including ones that cannot be read
directly, like LinkedIn and Indeed - so it is the widest single source available.

Cost: SerpApi's free plan is 250 searches a month. This makes ONE search per query
term per scan (10 results each) - four terms is ~60 scans a month - and the shared
HTTP cache means a re-scan within 30 minutes costs nothing. Raise PAGES_PER_QUERY only
with a paid plan.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from ..http import SESSION
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.google_jobs")

ENDPOINT = "https://serpapi.com/search.json"

_AGO = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)
_UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}


def posted_date(text: str | None, today: datetime | None = None) -> str | None:
    """'3 days ago' -> an ISO date. Anything else goes to the normal parser."""
    match = _AGO.search(text or "")
    if not match:
        return parse_date(text) if text else None
    days = int(match.group(1)) * _UNIT_DAYS[match.group(2).lower()]
    return ((today or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(
        timespec="seconds")


class GoogleJobsSource(BaseAggregator):
    name = "google_jobs"
    per_market = True                  # one search per country (gl=...)
    kind = "aggregator"
    needs_key = True
    PAGES_PER_QUERY = 1

    def available(self) -> bool:
        return bool(os.environ.get("SERPAPI_KEY"))

    def _search(self, params: dict) -> dict | None:
        data = SESSION.get_json(ENDPOINT, params=params, check_robots=False)
        if data and data.get("error") and "location" in str(data["error"]).lower() \
                and "location" in params:
            # An unrecognised place name - retry country-wide rather than give up.
            params = {k: v for k, v in params.items() if k != "location"}
            data = SESSION.get_json(ENDPOINT, params=params, check_robots=False)
        if data and data.get("error"):
            log.warning("google_jobs: %s", data["error"])
            return None
        return data

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        if not self.available() or not query:
            return []                     # an empty query would spend a search on nothing
        params = {"engine": "google_jobs", "q": query, "hl": "en",
                  "api_key": os.environ["SERPAPI_KEY"]}
        if home:
            params["gl"] = home.code.lower()
        if location:
            params["location"] = f"{location}, {home.name}" if home else location

        jobs: list[Job] = []
        try:
            for _ in range(self.PAGES_PER_QUERY):
                data = self._search(params)
                if not data:
                    break
                for item in data.get("jobs_results") or []:
                    options = item.get("apply_options") or []
                    url = (options[0].get("link") if options else "") or item.get("share_link", "")
                    if not url or not item.get("title"):
                        continue          # never invent a link
                    ext = item.get("detected_extensions") or {}
                    jobs.append(Job(
                        source=self.name, source_kind=self.kind,
                        company=item.get("company_name", ""), title=item["title"],
                        url=url, external_id=str(item.get("job_id", ""))[:120],
                        location=item.get("location", ""),
                        remote=bool(ext.get("work_from_home")),
                        description=strip_html(item.get("description", "")),
                        salary_raw=ext.get("salary", "") or "",
                        employment_type=ext.get("schedule_type", "") or "",
                        posted_at=posted_date(ext.get("posted_at")),
                        raw={"via": item.get("via", ""),
                             "apply_via": [o.get("title") for o in options][:5]},
                    ))
                token = (data.get("serpapi_pagination") or {}).get("next_page_token")
                if not token or len(jobs) >= max_results:
                    break
                params = dict(params, next_page_token=token)
        except Exception as exc:
            log.warning("google_jobs search failed: %s", exc)
        return jobs[:max_results]
