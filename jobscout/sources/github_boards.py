"""Community-maintained GitHub internship and new-grad trackers.

Fully public READMEs on raw.githubusercontent.com (a static CDN, not a crawled page) -
where a lot of students actually look. Checked live on 2026-10-01; each repo below
parsed to real rows with company, title and apply link.

Formats differ a lot between maintainers, so nothing here assumes column positions:

  * HTML <table>s (SimplifyJobs) - the Application cell holds the real ATS link first,
    then a simplify.jobs wrapper; the first is taken.
  * Markdown pipe tables - the header row is read to find the company, role, location
    and link columns, whatever they are called and in whatever order ("Apply",
    "Application", "link", or no link column at all with the title itself linked).
    Links may be HTML <a href>, Markdown [text](url), or a badge-image button
    [![Apply](badge.svg)](url) - image URLs are never mistaken for the posting.
  * One section per firm (northwesternfintech's quant list) - a "## Firm" heading, a
    "**Locations**:" line, then a Role | Links table where each open link is a role.

Seasons roll over: "{year}" in a repo name is tried for this year and next, so the
Summer 2028 lists are picked up on the first scan after they appear, with no code
change. A repo that does not exist on any branch is skipped (one dead entry is kept on
purpose so that path stays tested for real).
"""
from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from ..http import SESSION
from ..models import Job
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.github_boards")

# (repo, what it lists, location to assume when the table has no location column)
_REPO_SPECS = (
    ("SimplifyJobs/Summer{year}-Internships", "Internship", ""),
    ("vanshb03/Summer{year}-Internships", "Internship", ""),
    ("SimplifyJobs/New-Grad-Positions", "New grad", ""),
    ("zapplyjobs/Internships-{year}", "Internship", ""),
    ("zapplyjobs/awesome-ml-internships-{year}", "Internship", ""),
    ("negarprh/Canadian-Tech-Internships-{year}", "Internship", ""),
    ("LorenzoLaCorte/european-tech-internships-{year}", "Internship", ""),
    ("didtheyghostme/Singapore-Summer{year}-TechInternships", "Internship", "Singapore"),
    ("jobright-ai/{year}-Software-Engineer-New-Grad", "New grad", ""),
    ("jobright-ai/{year}-Data-Analysis-New-Grad", "New grad", ""),
    ("jobright-ai/{year}-Product-Management-New-Grad", "New grad", ""),
    # Engineering, computing and research lists (added 2026-10-05, each parsed live):
    # hardware/mechanical/electrical/civil internships, software and data internships,
    # SWE and AI/ML college roles, hardware early-career, and quant internships.
    ("jobright-ai/{year}-Engineer-Internship", "Internship", ""),
    ("jobright-ai/{year}-Software-Engineer-Internship", "Internship", ""),
    ("jobright-ai/{year}-Data-Analysis-Internship", "Internship", ""),
    ("speedyapply/{year}-SWE-College-Jobs", "Internship", ""),
    ("speedyapply/{year}-AI-College-Jobs", "Internship", ""),
    ("zapplyjobs/New-Grad-Hardware-Engineering-Jobs-{year}", "New grad", ""),
    ("northwesternfintech/{year}QuantInternships", "Internship", ""),
    ("Ouckah/Summer2026-Internships", "Internship", ""),     # gone - see docstring
)
_BRANCHES = ("dev", "main", "master")


def expand_repos(today: date | None = None) -> list[tuple[str, str, str]]:
    """Concrete repo names for this season and next, de-duplicated, order kept."""
    year = (today or date.today()).year
    out: list[tuple[str, str, str]] = []
    for pattern, kind, where in _REPO_SPECS:
        for y in ((year, year + 1) if "{year}" in pattern else (None,)):
            name = pattern.format(year=y) if y else pattern
            if name not in {r for r, _, _ in out}:
                out.append((name, kind, where))
    return out


_REPOS = tuple(r for r, _, _ in expand_repos())

# Legend glyphs (FAANG+, no sponsorship, citizenship...) and the "same company as the
# row above" arrow decorate rows; none of it belongs in a title or company name.
_DECORATION_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF↖-⇿️]")
_CONTINUATION = "↳"
_BR_RE = re.compile(r"</?br\s*/?>", re.I)
# Link text may hold one level of brackets of its own: "[[2027] Software Engineer](url)".
_MD_LINK_RE = re.compile(r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(([^)\s]+)\)")
_HREF_RE = re.compile(r'href="([^"]+)"')
_IMAGE_URL_RE = re.compile(r"img\.shields\.io|\.(png|svg|gif|jpe?g|webp)(\?|$)|/images?/",
                           re.I)
_CLOSED = "\U0001F512"            # lock emoji


def _cell_text(fragment: str, sep: str = " ") -> str:
    """Plain text of one cell: Markdown links reduced to their text, HTML stripped.
    Tolerates the stray '</br>' some rows use between locations."""
    fragment = _BR_RE.sub(sep, fragment or "")
    fragment = _MD_LINK_RE.sub(lambda m: m.group(1), fragment)
    return BeautifulSoup(fragment, "lxml").get_text(" ", strip=True)


def _clean(text: str) -> str:
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text or "").replace("**", "")
    return re.sub(r"\s+", " ", _DECORATION_RE.sub(" ", text)).strip()


def _links(fragment: str) -> list[str]:
    """Every posting URL in a cell, images excluded, in order of appearance."""
    found = []
    for match in re.finditer(r'href="([^"]+)"|\]\(([^)\s]+)\)', fragment or ""):
        url = (match.group(1) or match.group(2) or "").strip()
        if url.startswith(("http://", "https://")) and not _IMAGE_URL_RE.search(url):
            found.append(url)
    return found


def _first_href(fragment: str) -> str:
    links = _links(fragment)
    return links[0] if links else ""


def _rows_from_html_tables(text: str) -> list[dict]:
    """SimplifyJobs' format: a real <table>/<tbody> per job category."""
    rows = []
    soup = BeautifulSoup(text, "lxml")
    for table in soup.find_all("table"):
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            company_cell, role_cell, location_cell, application_cell = cells[:4]
            link = application_cell.find("a")
            rows.append({
                "company_raw": _cell_text(str(company_cell)),
                "title_raw": _cell_text(str(role_cell)),
                "location_raw": _cell_text(str(location_cell), ", "),
                "url": link.get("href", "").strip() if link else "",
                "closed": _CLOSED in tr.get_text(),
            })
    return rows


_PIPE_ROW = re.compile(r"^\|(.+)\|\s*$")
_HEADER_WORDS = {
    "company": ("company", "employer", "organisation", "organization"),
    "title": ("role", "title", "job title", "position", "job", "internship", "opportunity"),
    "location": ("location", "locations", "city", "where"),
    "link": ("apply", "application", "link", "posting", "url", "apply link"),
}


def _header_map(cells: list[str]) -> dict[str, int] | None:
    """Which column is which, from a header row - or None if it is not a job table."""
    names = [_clean(_cell_text(c)).lower().strip(" :") for c in cells]
    found: dict[str, int] = {}
    for field, words in _HEADER_WORDS.items():
        # Exact header first ("Role"), then one that contains a word ("Application/Link").
        for test in (lambda n: n in words, lambda n: any(w in n for w in words)):
            hit = next((i for i, n in enumerate(names)
                        if n and test(n) and i not in found.values()), None)
            if hit is not None:
                found[field] = hit
                break
    return found if {"company", "title"} <= set(found) else None


def _rows_from_pipe_tables(text: str) -> list[dict]:
    """Markdown pipe tables, columns located by their header row."""
    rows = []
    columns: dict[str, int] | None = None
    for line in text.splitlines():
        m = _PIPE_ROW.match(line.strip())
        if not m:
            columns = None                 # a blank or prose line ends the table
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                       # the |---|---| underline
        header = _header_map(cells)
        if header:
            columns = header
            continue
        if not columns or len(cells) <= max(columns.values()):
            continue

        def cell(field: str) -> str:
            return cells[columns[field]] if field in columns else ""

        url = _first_href(cell("link")) or _first_href(cell("title"))
        rows.append({
            "company_raw": _cell_text(cell("company")),
            "title_raw": _cell_text(cell("title")),
            "location_raw": _cell_text(cell("location"), ", "),
            "url": url,
            "closed": _CLOSED in line,
        })
    return rows


# The quant list abbreviates its roles; a title should read like one.
_ROLE_NAMES = {"QR": "Quantitative Researcher", "QT": "Quantitative Trader",
               "QD": "Quantitative Developer", "SWE": "Software Engineer",
               "HW": "Hardware Engineer", "ML": "Machine Learning Engineer",
               "FPGA": "FPGA Engineer"}
_FIRM_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _rows_from_firm_sections(text: str) -> list[dict]:
    """"## Firm", "**Locations**: NYC", then |Role|Links| rows whose links are
    "[✅ C++](url)" - one open role per link, its label naming the variant."""
    rows, company, location = [], "", ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            company, location = _clean(line[3:]), ""
        elif line.startswith("**Locations**:"):
            location = _clean(line.split(":", 1)[1])
        elif company and line.startswith("|") and "](" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            role = _clean(cells[0])
            role = _ROLE_NAMES.get(role.upper(), role)
            for label, url in _FIRM_LINK.findall(cells[1]):
                if "\u2705" not in label:          # only the ticked (open) links
                    continue
                variant = _clean(label.replace("\u2705", ""))
                rows.append({
                    "company_raw": company, "location_raw": location, "url": url,
                    "title_raw": f"{role} Intern" + (f" ({variant})" if variant else ""),
                    "closed": False,
                })
    return rows


class GitHubInternshipsSource(BaseAggregator):
    """Community-run internship and new-grad trackers on GitHub."""

    name = "github_internships"
    uses_query = False
    kind = "board"
    needs_key = False

    branches = _BRANCHES
    repos = _REPOS            # as of import; fetch() re-expands, so seasons roll over

    def _fetch_readme(self, repo: str) -> str | None:
        for branch in self.branches:
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/README.md"
            text = SESSION.get_text(url, check_robots=False)
            if text:
                return text
        return None

    def _jobs_from(self, repo: str, kind: str, default_location: str) -> list[Job]:
        text = self._fetch_readme(repo)
        if not text:
            log.info("github_internships: no README for %s on any branch - skipping", repo)
            return []
        jobs = []
        last_company = ""
        rows = _rows_from_html_tables(text) + _rows_from_pipe_tables(text)
        if not rows:
            rows = _rows_from_firm_sections(text)
        for row in rows:
            if row["closed"]:
                continue
            raw_company = row["company_raw"]
            if _CONTINUATION in raw_company or not raw_company.strip():
                company = last_company
            else:
                company = _clean(raw_company)
                last_company = company
            title = _clean(row["title_raw"])
            url = row["url"].strip()
            if not company or not title or not url:
                continue                   # never invent a URL - unverifiable rows drop
            jobs.append(Job(
                source=self.name, source_kind=self.kind, company=company, title=title,
                url=url, location=_clean(row["location_raw"]) or default_location,
                employment_type=kind, raw={"repo": repo},
            ))
        return jobs

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, **kwargs) -> list[Job]:
        per_repo: list[list[Job]] = []
        for repo, kind, where in expand_repos():
            try:
                found = self._jobs_from(repo, kind, where)
            except Exception as exc:
                log.warning("github_internships: %s failed (%s)", repo, exc)
                continue
            if found:
                per_repo.append(found)
        # Round-robin across repos, so the budget is shared rather than spent entirely
        # on whichever README happens to be first; README order is newest-first, so
        # each repo contributes its freshest rows. The same role listed by several
        # trackers (often under different wrapper links) is kept once.
        jobs: list[Job] = []
        seen: set[str] = set()
        depth = 0
        while len(jobs) < max_results and any(depth < len(r) for r in per_repo):
            for rows in per_repo:
                if depth < len(rows) and rows[depth].dedupe_key not in seen:
                    seen.add(rows[depth].dedupe_key)
                    jobs.append(rows[depth])
                    if len(jobs) >= max_results:
                        break
            depth += 1
        return jobs
