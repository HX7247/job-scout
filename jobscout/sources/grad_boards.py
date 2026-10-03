"""Graduate/internship boards and the Careerjet search API.

Feasibility was checked live on 2026-09-28 for every board students commonly use; only
the ones that pass are implemented, and the rest are recorded here so nobody re-tries
them blind:

  GradConnection (au./nz./sg.gradconnection.com)  FEASIBLE. robots.txt allows the
      listing pages (only */track-link, */calendar and /universities/* are disallowed);
      a plain GET of /internships/ or /graduate-jobs/ returns 20 server-rendered
      `.campaign-box` cards per page, ?page=N paginates. uk.gradconnection.com
      redirects to the Singapore edition, so this serves AU, NZ and SG only.
  Careerjet search API  FEASIBLE with a free publisher key (CAREERJET_API_KEY): the
      documented v4 endpoint answers a keyless call with a structured 401. It is a job
      search engine indexing many boards, and the legitimate route to the kind of
      breadth people go to Indeed for. It has a contract_type=i (internship) filter.
  Prospects (prospects.ac.uk)  NOT FEASIBLE: listings are rendered client-side; the
      page HTML has no postings at all.
  TargetJobs (targetjobs.co.uk)  NOT FEASIBLE: robots.txt allows everything, but
      search results are rendered client-side; no postings in the HTML.
  Milkround (milkround.com)  NOT FEASIBLE: every request times out (bot management).
  Jooble API  NOT FEASIBLE from here: even the API endpoint returns a Cloudflare
      "Just a moment..." browser challenge.
  Civil Service Jobs  NOT FEASIBLE: results sit behind a session-keyed search form.
  Indeed, Glassdoor, LinkedIn  never attempted: their terms prohibit scraping and
      they actively block it.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import SESSION, USER_AGENT
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.grad_boards")


class GradConnectionSource(BaseAggregator):
    """Australia/NZ/Singapore's main graduate and internship board."""

    name = "gradconnection"
    kind = "board"
    needs_key = False
    uses_query = False            # category pages, not a keyword search
    markets = ("AU", "NZ", "SG")

    HOSTS = {"AU": "au.gradconnection.com", "NZ": "nz.gradconnection.com",
             "SG": "sg.gradconnection.com"}
    CATEGORIES = (("internships", "Internship"), ("graduate-jobs", "Graduate job"))
    PAGES = 5

    # The card's own label for what the listing is. Events and open days are listed
    # alongside jobs; they are not something you can apply to for a role.
    _SKIP_TYPES = re.compile(r"\b(event|webinar|open day|competition|program info)\b", re.I)

    def _parse(self, html: str, base: str, default_type: str) -> list[Job]:
        soup = BeautifulSoup(html, "lxml")
        jobs = []
        for box in soup.select(".campaign-box"):
            for modal in box.select(".modal-background"):
                modal.decompose()           # the "sign up to apply" modal, not the listing
            link = box.select_one("a.box-header-title")
            if not link or not link.get("href"):
                continue
            title = link.get_text(" ", strip=True)
            employer = box.select_one(".box-employer-name")
            company = employer.get_text(" ", strip=True) if employer else ""
            label = box.select_one(".job-description-row .ellipsis-text-paragraph")
            kind = label.get_text(" ", strip=True) if label else default_type
            if self._SKIP_TYPES.search(kind):
                continue
            place = box.select_one(".location-name")
            all_places = [li.get_text(strip=True) for li in (place.select(".tooltip-list li")
                                                             if place else [])]
            if all_places:
                location = ", ".join(all_places)
            else:
                location = place.get_text(" ", strip=True) if place else ""
            remote = bool(box.select_one(".box-remote-tags .remote"))
            international = bool(box.select_one(".accept-international"))
            closing = box.select_one(".closing-in")
            desc_bits = [kind, f"Location: {location}" if location else "",
                         "Accepts international applicants" if international else "",
                         closing.get_text(" ", strip=True) if closing else ""]
            jobs.append(Job(
                source=self.name, source_kind=self.kind, company=company, title=title,
                url=urljoin(base, link["href"]),
                location=location or ("Remote" if remote else ""), remote=remote,
                employment_type=("Internship" if "intern" in kind.lower()
                                 else "Graduate scheme" if "grad" in kind.lower() else kind),
                description=strip_html(". ".join(b for b in desc_bits if b)),
                raw={"accepts_international": international},
            ))
        return jobs

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        code = home.code if home and home.code in self.HOSTS else "AU"
        if home and not self.serves(home):
            return []
        base = f"https://{self.HOSTS[code]}"
        jobs: list[Job] = []
        seen: set[str] = set()
        per_category = max(1, max_results // len(self.CATEGORIES))
        for slug, default_type in self.CATEGORIES:
            got = 0
            for page in range(1, self.PAGES + 1):
                if got >= per_category:
                    break
                try:
                    html = SESSION.get_text(f"{base}/{slug}/",
                                            params={"page": page} if page > 1 else None,
                                            check_robots=True)
                    if not html:
                        break
                    found = self._parse(html, base, default_type)
                except Exception as exc:
                    log.warning("gradconnection %s page %s failed: %s", slug, page, exc)
                    break
                if not found:
                    break
                # The same role is listed once per degree discipline it accepts, each
                # under its own URL (Citadel's quant trading role appeared 12 times in
                # 60 rows), so de-duplicate on the store's own identity, not the URL,
                # or duplicates eat the page budget.
                for job in found:
                    if job.dedupe_key not in seen and got < per_category:
                        seen.add(job.dedupe_key)
                        jobs.append(job)
                        got += 1
        return jobs[:max_results]


class CareerjetSource(BaseAggregator):
    """Careerjet's documented v4 search API - a free publisher key is required."""

    name = "careerjet"
    kind = "aggregator"
    needs_key = True

    ENDPOINT = "https://search.api.careerjet.net/v4/query"
    LOCALES = {"GB": "en_GB", "US": "en_US", "AU": "en_AU", "NZ": "en_NZ",
               "CA": "en_CA", "IE": "en_IE", "IN": "en_IN", "SG": "en_SG",
               "ZA": "en_ZA", "DE": "de_DE", "FR": "fr_FR", "NL": "nl_NL",
               "ES": "es_ES", "IT": "it_IT"}
    markets = tuple(LOCALES)

    def available(self) -> bool:
        return bool(os.environ.get("CAREERJET_API_KEY"))

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        if not self.available() or (home and not self.serves(home)):
            return []
        token = base64.b64encode(f"{os.environ['CAREERJET_API_KEY']}:".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        locale = self.LOCALES.get(home.code if home else "GB", "en_GB")
        # The API asks for the IP and user agent of the person the search is for. On a
        # program running on your own machine that person is you, on this machine.
        base_params = {"locale_code": locale, "keywords": query, "page_size": 100,
                       "sort": "date", "user_ip": os.environ.get("CAREERJET_USER_IP",
                                                                 "127.0.0.1"),
                       "user_agent": USER_AGENT}
        if re.search(r"\bintern", query or "", re.I):
            base_params["contract_type"] = "i"
        jobs: list[Job] = []
        try:
            page = 1
            while page <= 10:
                params = dict(base_params, page=page)
                if location:
                    params["location"] = location
                data = SESSION.get_json(self.ENDPOINT, params=params, headers=headers,
                                        check_robots=False)
                if data and data.get("type") == "LOCATIONS" and location:
                    location = ""           # ambiguous place name: same page, country-wide
                    continue
                page += 1
                items = (data or {}).get("jobs") or []
                for item in items:
                    jobs.append(Job(
                        source=self.name, source_kind=self.kind,
                        company=item.get("company", ""), title=item.get("title", ""),
                        url=item.get("url", ""), location=item.get("locations", ""),
                        description=strip_html(item.get("description", "")),
                        salary_raw=item.get("salary", ""),
                        salary_min=item.get("salary_min") if item.get("salary_type") == "Y" else None,
                        salary_max=item.get("salary_max") if item.get("salary_type") == "Y" else None,
                        salary_currency=item.get("salary_currency_code", ""),
                        posted_at=parse_date(item.get("date")),
                        raw={"salary_type": item.get("salary_type")},
                    ))
                if len(jobs) >= max_results or page > int((data or {}).get("pages") or 0):
                    break
        except Exception as exc:
            log.warning("careerjet search failed: %s", exc)
        return [j for j in jobs if j.url and j.title][:max_results]


GRAD_BOARD_ADAPTERS = {a.name: a() for a in [GradConnectionSource, CareerjetSource]}
