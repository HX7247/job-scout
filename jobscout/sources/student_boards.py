"""UK student job-board adapters: Gradcracker, RateMyPlacement, Bright Network.

These are HTML pages, not JSON APIs, so each adapter is checked for real feasibility
(robots.txt + an actual unauthenticated fetch) before being wired up - see the class
docstrings below for what was found and when (2026-09-27). A site that fails either
check gets an honest `return []` rather than a scraper that quietly never finds
anything: same "zero fabrication, honest degradation" principle as the rest of the
app (see jobscout/review.py).

Feasibility findings (2026-09-27):
  * Gradcracker (gradcracker.com)      - FEASIBLE. robots.txt allows the search pages
    used here; a plain GET returns real job cards with company/title/url/location in
    the static HTML (Livewire-rendered server-side, not client-only).
  * RateMyPlacement (ratemyplacement.co.uk) - the brand has migrated: every URL on
    ratemyplacement.co.uk, including /robots.txt, 301-redirects to https://higherin.com/
    (same company, "Higherin: Student Jobs, Careers Advice & Job Reviews"). higherin.com
    has its own real robots.txt (general crawling allowed) and a plain GET on its
    search-jobs pages embeds the full result set as a JSON blob
    (`window.__RMP_SEARCH_RESULTS_INITIAL_STATE__`) directly in the HTML - so this is
    FEASIBLE, just against the site's current live domain rather than the old one.
    Free-text query filtering only works client-side against `/ajax/...`, which
    robots.txt disallows for everyone, and which returned identical results to the
    unfiltered page when tried anyway - so this adapter ignores free-text `query` and
    always fetches the placements + internships categories, which is the right
    default for this app's UK-student use case.
  * Bright Network (brightnetwork.co.uk) - NOT FEASIBLE. Every request, including a
    bare GET of /robots.txt, is intercepted by a Cloudflare JS challenge ("Just a
    moment... Enable JavaScript and cookies to continue", HTTP 403). There is no
    plain-HTML listing to parse and no way to pass the challenge without executing
    JavaScript, which is out of scope here. `fetch()` returns [] unconditionally.
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..http import SESSION
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.student_boards")

# Same model constant used everywhere else in this codebase (interview.py, why.py,
# assistant.py, apply.py, review.py) - kept identical for consistency even though the
# name looks unusual.
MODEL = "claude-opus-5"

_ORDINAL = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)


def _clean_date(text: str) -> str | None:
    """parse_date() understands '27 September 2026' but not '27th September 2026'."""
    if not text:
        return None
    return parse_date(_ORDINAL.sub(r"\1", text).replace(",", ""))


def _strip_noise(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()
    return soup


def _fragment_for_llm(html: str, max_chars: int = 12000) -> str:
    """A trimmed, script-free slice of the page for the LLM fallback - full pages here
    run to 1MB+, most of it markup noise or nav/footer boilerplate that would blow the
    token budget for no benefit."""
    try:
        soup = _strip_noise(BeautifulSoup(html, "lxml"))
        body = soup.body or soup
        return str(body)[:max_chars]
    except Exception:
        return html[:max_chars]


# --------------------------------------------------------------- LLM fallback agent
def _extract_listings_via_llm(html_fragment: str, source_name: str) -> list[dict] | None:
    """Ask Claude to pull job listings out of a chunk of raw listing-page HTML.

    This exists as insurance against a future redesign silently breaking the
    hand-written parser below - it is never the primary path. Returns None when no
    key is configured (the direct parser already makes each adapter usable without
    one), or when the call fails for any reason. Every returned ``url`` is verified as
    a real, verbatim substring of the HTML we sent - anything the model paraphrased,
    normalised or invented is dropped silently, mirroring the evidence-citation
    discipline in jobscout/review.py's `_verify_evidence`.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    if not html_fragment or not html_fragment.strip():
        return None

    tool = {
        "name": "extract_job_listings",
        "description": f"Extract job/placement/internship listing cards from a fragment "
                       f"of {source_name}'s search-results page HTML.",
        "input_schema": {
            "type": "object",
            "properties": {
                "listings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string",
                                        "description": "The employer name shown on the card."},
                            "title": {"type": "string",
                                      "description": "The job/placement/internship title "
                                                     "shown on the card."},
                            "url": {"type": "string",
                                    "description": "The link to the posting's own page. "
                                                   "Must be copied character-for-character "
                                                   "from an href in the provided HTML - "
                                                   "never invented or guessed."},
                            "location": {"type": "string",
                                        "description": "The location text on the card, if "
                                                       "any shown. Empty string if none."},
                        },
                        "required": ["company", "title", "url"],
                    },
                },
            },
            "required": ["listings"],
        },
    }

    prompt = f"""The HTML below is a fragment of a job-search results page from
{source_name}, a UK student job board. It contains zero or more job / placement /
internship listing cards.

Extract every distinct listing you can find. For each one, give the company name, the
job title, its location if shown, and its url.

CRITICAL: "url" must be copied character-for-character from an href attribute that is
actually present in the HTML below - never invented, guessed, normalised, or
reconstructed from a pattern. If you cannot find a real href for a listing, omit that
listing entirely rather than guess at its URL.

HTML:
{html_fragment}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL, max_tokens=4000,
            tools=[tool], tool_choice={"type": "tool", "name": "extract_job_listings"},
            messages=[{"role": "user", "content": prompt}])
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                listings = block.input.get("listings", [])
                verified = []
                for item in listings:
                    url = (item.get("url") or "").strip()
                    if url and url in html_fragment:
                        verified.append(item)
                    else:
                        log.info("%s: dropping LLM-extracted listing, url not found "
                                 "verbatim in source HTML", source_name)
                return verified
    except Exception as exc:
        log.warning("Claude unavailable for %s listing extraction: %s", source_name, exc)
    return None


# ------------------------------------------------------------------- Gradcracker
class GradcrackerSource(BaseAggregator):
    """STEM-focused UK placement/internship/graduate board.

    robots.txt (gradcracker.com) allows `/search/...` pages generally; it specifically
    disallows `/keyword-search` (the site's live-typeahead endpoint, which we never
    call), `/search/*?order=` / `&order=` (a sort-order query param, which we never
    set), and the single sponsored slot `/hub/9000001` (skipped below - see
    `_SPONSORED_SLOT`). A plain GET of a real search page (verified against
    https://www.gradcracker.com/search/computing-technology/jobs and
    .../all-disciplines/engineering-work-placements-internships on 2026-09-27) returns
    real job cards - company, title, url, location, subject and deadline/salary - as
    server-rendered Livewire `<article wire:key="...">` blocks in the plain HTML. No
    login wall, no client-side-only rendering.
    """

    name = "gradcracker"
    kind = "board"
    needs_key = False
    markets = ("GB",)

    BASE = "https://www.gradcracker.com"
    # Discipline slugs actually present on the site (verified live), used as a
    # best-effort mapping when a free-text query looks like it names a field. There is
    # no free-text search endpoint reachable without hitting the disallowed
    # /keyword-search path, so this is the closest a query can get to filtering.
    _KEYWORD_DISCIPLINE = {
        "software": "computing-technology", "computer": "computing-technology",
        "programming": "computing-technology", "data": "computing-technology",
        "tech": "computing-technology", "it": "computing-technology",
        "mechanical": "mechanical-manufacturing", "manufacturing": "mechanical-manufacturing",
        "civil": "civil-building", "construction": "civil-building", "building": "civil-building",
        "electrical": "electronic-electrical", "electronic": "electronic-electrical",
        "aerospace": "aerospace", "aeronautical": "aerospace",
        "chemical": "chemical-process", "process": "chemical-process",
        "science": "science", "physics": "science", "chemistry": "science", "biology": "science",
        "maths": "maths-business", "mathematics": "maths-business", "business": "maths-business",
        "finance": "maths-business", "economics": "maths-business", "accounting": "maths-business",
        "engineer": "all-disciplines", "engineering": "all-disciplines",
    }
    _DEFAULT_DISCIPLINES = ("computing-technology", "all-disciplines", "science", "maths-business")
    _SPONSORED_SLOT = "/hub/9000001"  # the one path robots.txt explicitly disallows

    def _disciplines_for(self, query: str) -> list[str]:
        if not query:
            return list(self._DEFAULT_DISCIPLINES)
        words = re.findall(r"[a-z]+", query.lower())
        matched = [self._KEYWORD_DISCIPLINE[w] for w in words if w in self._KEYWORD_DISCIPLINE]
        seen: list[str] = []
        for slug in matched or self._DEFAULT_DISCIPLINES:
            if slug not in seen:
                seen.append(slug)
        return seen

    def _parse_page(self, html: str, page_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[Job] = []
        for art in soup.find_all("article", attrs={"wire:key": True}):
            h2 = art.find("h2")
            a = h2.find("a", href=True) if h2 else None
            if not a:
                continue
            url = urljoin(page_url, a["href"].strip())
            if self._SPONSORED_SLOT in url:
                continue  # robots.txt-disallowed sponsored placeholder, not a real ad
            title = a.get_text(strip=True)
            if not title:
                continue

            company = ""
            figure = art.find("figure")
            if figure:
                img = figure.find("img")
                if img and img.get("alt"):
                    company = img["alt"].strip()

            fields: dict[str, str] = {}
            dl = art.find("dl")
            if dl:
                for dt in dl.find_all("dt"):
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        fields[dt.get_text(strip=True)] = dd.get_text(strip=True)

            h3 = art.find("h3")
            subject = h3.get_text(strip=True) if h3 else ""

            path_parts = urlparse(url).path.strip("/").split("/")
            # .../hub/{id}/{company-slug}/{type}/{job-id}/{title-slug}
            employment_type = ""
            if "work-placement-internship" in path_parts:
                employment_type = "Placement/Internship"
            elif "graduate-job" in path_parts:
                employment_type = "Graduate"
            elif "degree-apprenticeship" in path_parts:
                employment_type = "Degree Apprenticeship"

            desc_bits = [b for b in [
                f"Subjects: {subject}" if subject else "",
                f"Salary: {fields['Salary']}" if fields.get("Salary") else "",
                f"Duration: {fields['Duration']}" if fields.get("Duration") else "",
                f"Starting: {fields['Starting']}" if fields.get("Starting") else "",
                f"Degree required: {fields['Degree required']}" if fields.get("Degree required") else "",
                f"Deadline: {fields['Deadline']}" if fields.get("Deadline") else "",
            ] if b]

            job_id_match = re.search(r"/(\d+)/[a-z0-9-]+/?$", url)
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company, title=title, url=url,
                external_id=job_id_match.group(1) if job_id_match else "",
                location=fields.get("Location", ""),
                description=strip_html("\n".join(desc_bits)),
                salary_raw=fields.get("Salary", ""),
                salary_currency="GBP" if fields.get("Salary") else "",
                employment_type=employment_type,
                department=subject,
                closes_at=_clean_date(fields.get("Deadline", "")),
                raw=fields,
            ))
        return jobs

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        if home and not self.serves(home):
            log.info("Gradcracker only covers the UK - skipping for %s", home.name)
            return []
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        try:
            for slug in self._disciplines_for(query):
                for page in (1, 2):
                    if len(jobs) >= max_results:
                        break
                    url = f"{self.BASE}/search/{slug}/work-placements-internships"
                    html = SESSION.get_text(url, params={"page": page} if page > 1 else None,
                                            check_robots=True)
                    if not html:
                        break  # robots-disallowed, or the fetch failed - stop this discipline
                    try:
                        page_jobs = self._parse_page(html, url)
                    except Exception as exc:
                        log.warning("Gradcracker: direct parse failed for %s: %s", url, exc)
                        page_jobs = []
                    if not page_jobs and page == 1:
                        # The direct parser is written against today's real markup; zero
                        # results on a page that should have some means a redesign broke
                        # it, so fall back to the LLM extraction agent as insurance.
                        fallback = _extract_listings_via_llm(_fragment_for_llm(html), self.name)
                        if fallback:
                            for item in fallback:
                                page_jobs.append(Job(
                                    source=self.name, source_kind=self.kind,
                                    company=item.get("company", ""), title=item.get("title", ""),
                                    url=urljoin(url, item.get("url", "")),
                                    location=item.get("location", ""),
                                    raw={"extracted_by": "llm_fallback"},
                                ))
                    if not page_jobs:
                        break
                    for job in page_jobs:
                        if job.url not in seen_urls:
                            seen_urls.add(job.url)
                            jobs.append(job)
                if len(jobs) >= max_results:
                    break
        except Exception as exc:
            log.warning("Gradcracker fetch failed, degrading to empty list: %s", exc)
            return jobs[:max_results]
        return jobs[:max_results]


# --------------------------------------------------------------- RateMyPlacement
class RateMyPlacementSource(BaseAggregator):
    """UK placement-year board with company reviews - now trading as "Higherin".

    Every URL on the old ratemyplacement.co.uk domain, including /robots.txt itself,
    301-redirects to https://higherin.com/ (confirmed 2026-09-27: `curl -IL
    https://www.ratemyplacement.co.uk/robots.txt` lands on higherin.com's homepage).
    higherin.com is the same company/service under its current name, with its own real
    robots.txt (general-purpose crawling is allowed; only `/ajax/`, `/api/`, `/redirect`
    and a few report/moderation endpoints are disallowed for `User-agent: *`). A plain
    GET of e.g. https://higherin.com/search-jobs/placements returns the full result set
    embedded as JSON in a `window.__RMP_SEARCH_RESULTS_INITIAL_STATE__ = {...}` script
    tag in the static HTML - no login wall, no headless JS needed to read it.

    Free-text filtering (`?q=...`) is NOT applied server-side: a request with `?q=
    marketing` returned byte-for-byte the same 20 postings (and the same
    `totalResults` count) as no query at all, so real query filtering must happen
    client-side against an `/ajax/...` endpoint - which robots.txt disallows outright.
    Path-based category filtering, by contrast, is real and server-rendered
    (`/search-jobs/placements` narrows `totalResults` from ~1690 to ~688). So this
    adapter ignores free-text `query` and always fetches the `placements` and
    `internships` categories, which is the right default for a UK placement-year
    student and is honestly reported rather than pretending `query` works.
    """

    name = "ratemyplacement"

    uses_query = False
    kind = "board"
    needs_key = False
    markets = ("GB",)

    BASE = "https://higherin.com"
    _CATEGORIES = ("placements", "internships")
    _STATE_MARKER = "window.__RMP_SEARCH_RESULTS_INITIAL_STATE__ = "

    def _extract_state(self, html: str) -> dict | None:
        idx = html.find(self._STATE_MARKER)
        if idx == -1:
            return None
        start = idx + len(self._STATE_MARKER)
        try:
            data, _end = json.JSONDecoder().raw_decode(html, start)
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _job_from_item(self, item: dict) -> Job:
        job_type = item.get("jobTypeName", "")
        desc_bits = [b for b in [
            job_type,
            f"Relevant for: {item['relevantFor']}" if item.get("relevantFor") else "",
            f"Salary: {item['salary']}" if item.get("salary") else "",
            f"Deadline: {item['deadline']}" if item.get("deadline") else "",
        ] if b]
        return Job(
            source=self.name, source_kind=self.kind,
            company=item.get("companyName", ""),
            title=item.get("jobTitle", ""),
            url=item.get("url", ""),
            external_id=str(item.get("jobId", "")),
            location=item.get("jobLocationNames", ""),
            description=strip_html("\n".join(desc_bits)),
            salary_raw=item.get("salary", "") or "",
            salary_currency="GBP" if item.get("salary") else "",
            employment_type=job_type,
            department=item.get("relevantFor", ""),
            closes_at=_clean_date(item.get("deadline", "")),
            raw={"companyId": item.get("companyId")},
        )

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        if home and not self.serves(home):
            log.info("RateMyPlacement/Higherin only covers the UK - skipping for %s", home.name)
            return []
        if query:
            log.info("RateMyPlacement/Higherin: free-text query is not server-rendered "
                     "(only the JS-only /ajax/ endpoint filters, and robots.txt disallows "
                     "it) - ignoring '%s' and returning the general placements/internships "
                     "listing", query)
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        try:
            for category in self._CATEGORIES:
                # Up to 10 pages per category: this is the most placement-focused board
                # the app has, and three pages capped it at 120 of ~700 placements.
                # The short-page check below still stops early on the last page.
                for page in range(1, 11):
                    if len(jobs) >= max_results:
                        break
                    url = f"{self.BASE}/search-jobs/{category}"
                    html = SESSION.get_text(url, params={"page": page} if page > 1 else None,
                                            check_robots=True)
                    if not html:
                        break
                    state = None
                    try:
                        state = self._extract_state(html)
                    except Exception as exc:
                        log.warning("RateMyPlacement/Higherin: state parse failed for %s: %s",
                                   url, exc)
                    items = (state or {}).get("data", []) if state else []
                    if not items and page == 1:
                        fallback = _extract_listings_via_llm(_fragment_for_llm(html), self.name)
                        if fallback:
                            for item in fallback:
                                if item["url"] not in seen_ids:
                                    seen_ids.add(item["url"])
                                    jobs.append(Job(
                                        source=self.name, source_kind=self.kind,
                                        company=item.get("company", ""),
                                        title=item.get("title", ""),
                                        url=urljoin(url, item.get("url", "")),
                                        location=item.get("location", ""),
                                        raw={"extracted_by": "llm_fallback"},
                                    ))
                    if not items:
                        break
                    for item in items:
                        job_id = str(item.get("jobId", "")) or item.get("url", "")
                        if job_id in seen_ids:
                            continue
                        seen_ids.add(job_id)
                        jobs.append(self._job_from_item(item))
                    if len(items) < 20:  # short page == last page
                        break
                if len(jobs) >= max_results:
                    break
        except Exception as exc:
            log.warning("RateMyPlacement/Higherin fetch failed, degrading to empty list: %s", exc)
            return jobs[:max_results]
        return jobs[:max_results]


# ------------------------------------------------------------------ Bright Network
class BrightNetworkSource(BaseAggregator):
    """UK graduate/internship board and community.

    NOT FEASIBLE as a plain-HTTP scraper. Every request against brightnetwork.co.uk -
    including a bare GET of /robots.txt itself - is intercepted by a Cloudflare
    "Just a moment..." JS challenge page (verified 2026-09-27: both
    `curl https://www.brightnetwork.co.uk/robots.txt` and
    `curl https://www.brightnetwork.co.uk/graduate-jobs/` returned HTTP 403 with the
    Cloudflare challenge HTML, not robots rules or job listings). There is no HTML to
    parse and no way through the challenge without executing JavaScript in a real
    browser, which is out of scope for this HTTP-only adapter, so this returns an
    empty list unconditionally rather than a scraper that would never work.
    """

    name = "brightnetwork"

    uses_query = False
    kind = "board"
    needs_key = False
    markets = ("GB",)

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        # Cloudflare's JS challenge blocks every plain HTTP request to this site,
        # robots.txt included (see class docstring) - nothing to fetch here.
        return []


STUDENT_BOARD_ADAPTERS = {
    a.name: a() for a in [GradcrackerSource, RateMyPlacementSource, BrightNetworkSource]
}
