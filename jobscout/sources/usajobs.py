"""USAJobs - the official US federal government jobs API. Free registration at
developer.usajobs.gov; fully structured JSON, no scraping involved.

Auth is three HTTP headers, not a bearer token (confirmed against
developer.usajobs.gov/Guides/Authentication, and against the live endpoint - a
request with dummy values for all three reaches the real auth layer and gets back a
genuine, structured 401 rather than a network failure or a bot-firewall block):

  Host              data.usajobs.gov
  User-Agent        the email address you registered the key with (not a browser UA)
  Authorization-Key the API key itself

USAJobs' own PositionOfferingType codelist (data.usajobs.gov/api/codelist/positionofferingtypes,
which - like the other codelist endpoints - needs no auth at all) has an explicit
"Internships" value at code 15328, which is what this adapter filters on by default.
"""
from __future__ import annotations

import logging
import os

from ..http import SESSION
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.usajobs")

SEARCH_URL = "https://data.usajobs.gov/api/search"

# USAJobs' own documented code for "Internships" in the PositionOfferingType codelist.
INTERNSHIP_OFFERING_TYPE = "15328"


class USAJobsSource(BaseAggregator):
    """Official US federal jobs API. US-only by definition, key required."""

    name = "usajobs"
    kind = "aggregator"
    needs_key = True

    markets = ("US",)

    def available(self) -> bool:
        return bool(os.environ.get("USAJOBS_API_KEY") and os.environ.get("USAJOBS_USER_AGENT"))

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              internships_only: bool = True, home=None, **kwargs) -> list[Job]:
        if not self.available():
            return []
        if home and home.code not in self.markets:
            log.info("USAJobs only covers the US - skipping for %s", home.name)
            return []

        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": os.environ["USAJOBS_USER_AGENT"],
            "Authorization-Key": os.environ["USAJOBS_API_KEY"],
        }

        jobs, page = [], 1
        try:
            while len(jobs) < max_results and page <= 20:
                params = {
                    "Keyword": query,
                    "ResultsPerPage": 500,
                    "Page": page,
                }
                if location:
                    params["LocationName"] = location
                if internships_only:
                    params["PositionOfferingTypeCode"] = INTERNSHIP_OFFERING_TYPE
                data = SESSION.get_json(SEARCH_URL, params=params, headers=headers,
                                        check_robots=False)
                if not data:
                    break
                result = data.get("SearchResult") or {}
                items = result.get("SearchResultItems") or []
                if not items:
                    break
                for item in items:
                    desc = item.get("MatchedObjectDescriptor") or {}
                    locs = desc.get("PositionLocation") or []
                    location_name = (desc.get("PositionLocationDisplay")
                                     or ", ".join(l.get("LocationName", "") for l in locs))
                    remuneration = (desc.get("PositionRemuneration") or [{}])[0]
                    offering_types = desc.get("PositionOfferingType") or []
                    schedules = desc.get("PositionSchedule") or []
                    user_area = (desc.get("UserArea") or {}).get("Details") or {}
                    jobs.append(Job(
                        source=self.name, source_kind=self.kind,
                        company=desc.get("OrganizationName", "") or desc.get("DepartmentName", ""),
                        title=desc.get("PositionTitle", ""),
                        url=desc.get("PositionURI", ""),
                        external_id=str(desc.get("PositionID", "") or item.get("MatchedObjectId", "")),
                        location=location_name,
                        description=strip_html(user_area.get("JobSummary")
                                               or desc.get("UserArea", {}).get("Details", {}).get("MajorDuties", "")
                                               or desc.get("QualificationSummary", "")),
                        salary_raw=remuneration.get("Description", ""),
                        salary_min=remuneration.get("MinimumRange") or None,
                        salary_max=remuneration.get("MaximumRange") or None,
                        salary_currency="USD",
                        employment_type=", ".join(s.get("Name", "") for s in schedules if s.get("Name")),
                        department=desc.get("DepartmentName", ""),
                        posted_at=parse_date(desc.get("PublicationStartDate")),
                        closes_at=parse_date(desc.get("ApplicationCloseDate")),
                        raw={"offering_types": [o.get("Name") for o in offering_types],
                             "hiring_paths": desc.get("PositionSchedule")},
                    ))
                    if len(jobs) >= max_results:
                        break
                page_info = result.get("SearchResultCountAll")
                if page_info is not None and page * 500 >= page_info:
                    break
                page += 1
        except Exception as exc:
            log.warning("usajobs: search failed (%s)", exc)
            return []
        return jobs[:max_results]
