"""Search criteria, loaded from YAML with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def _build(cls, data: dict | None):
    """Construct a dataclass from YAML, ignoring keys it does not have.

    Needed because fields get removed as well as added. ``profile.name`` and
    ``profile.linkedin_url`` were both dropped when the app stopped collecting personal
    data, and an existing config.yaml still has them; a strict constructor would refuse
    to start rather than quietly do the right thing.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Profile:
    """Everything the app keeps about the person searching - and it is short on purpose.

    There is no name field, no email field and no LinkedIn URL field. The first two are
    not needed to match a job, and the third is an identifier the import does not require:
    linkedin_import.py reads the export file the user drops in, not their profile page.
    Anything richer than this - degree, skills, region - lives in the derived profile
    under data/accounts/, filtered by privacy.PROFILE_ALLOWLIST.
    """
    # Optional second input, when there is a CV to hand but no LinkedIn export yet.
    # Only skills and the qualification are read from it; the text is not kept.
    cv_path: str = ""
    school: str = ""            # public affiliation, drives the alumni deep-link
    school_linkedin_slug: str = ""
    graduation_year: str = ""
    # Your own tracker workbook, for the "append to my workbook" export.
    tracker_path: str = ""
    # Skills the vocabulary does not know yet. Yours to add, no approval needed.
    extra_skills: list[str] = field(default_factory=list)


@dataclass
class SearchCriteria:
    country: str = ""            # ISO code, e.g. "GB", "US", "DE": where you are
                                 # based. Drives currency, sponsorship and outreach.
                                 # Blank = inferred from the first location.
    # Countries whose jobs are listed (ISO codes). Where you are based is always one
    # of them. The Positions sidebar's Country filter narrows the list to one or two.
    markets: list[str] = field(default_factory=lambda: ["GB", "US", "AU"])
    titles: list[str] = field(default_factory=list)          # role names we want
    keywords_any: list[str] = field(default_factory=list)     # any one of these is a plus
    keywords_required: list[str] = field(default_factory=list)  # all must appear
    keywords_excluded: list[str] = field(default_factory=list)  # any kills the row
    locations: list[str] = field(default_factory=list)
    remote_ok: bool = True
    salary_min: int | None = None
    max_age_days: int | None = 60
    # Both are pure filters: empty means "no preference, show everything".
    # employment_types: part_time, full_time, internship, placement, contract,
    #   temporary/seasonal, apprenticeship, graduate_scheme, weekend, zero_hours...
    employment_types: list[str] = field(default_factory=list)
    # job_families: retail, hospitality, software, engineering, care, driving...
    job_families: list[str] = field(default_factory=list)
    # Keep postings that never state their hours or field. Turning this off hides
    # a lot of real jobs, because two thirds of postings say nothing.
    include_unclassified: bool = True
    # keep | strict - what to do when a posting's location resolves to no country
    # at all. Employers' own boards list bare town names ("Clydebank", "Corby"),
    # and no town list is ever complete. "keep" shows them and scores them lower;
    # "strict" drops anything not positively placed in your market.
    unknown_location: str = "keep"
    exclude_companies: list[str] = field(default_factory=list)
    # student | graduate | professional | senior | any
    # Drives level filtering, outreach tone and tracker defaults. Blank = infer
    # from the job titles, which is what older configs relied on.
    # Defaults to student: this is built for university students after placements,
    # internships and graduate schemes, and the Criteria screen already shows "Student"
    # for a blank value - a blank default meant it said Student while scoring as "any".
    # An explicit "" in a config file still means "infer from the titles".
    career_stage: str = "student"
    # any | prefer | require - for candidates who need work authorisation.
    # "require" drops employers with no sponsor licence on the official register;
    # "prefer" keeps them but ranks licensed employers higher.
    visa_sponsorship: str = "any"
    # remote | hybrid | onsite - empty means no preference. A posting that states
    # no arrangement is never dropped on this, only ranked lower.
    work_modes: list[str] = field(default_factory=list)
    exclude_senior: bool = True      # drop titles clearly above your level
    exclude_junior: bool = False     # drop titles clearly below it
    # Your own filters, for anything the built-in facets do not cover. Each is a label,
    # some terms, and require | exclude | boost. See rules.py; set facet: true and it
    # appears as a checkbox in the sidebar with its own count.
    custom_rules: list[dict] = field(default_factory=list)


@dataclass
class SourceConfig:
    use_ats: bool = True
    aggregators: list[str] = field(default_factory=lambda: [
        "arbeitnow", "remotive", "remoteok", "jobicy", "himalayas", "themuse",
        "adzuna", "reed",
        # Community/forum/student-board tier - see jobscout/sources/__init__.py's
        # COMMUNITY_ADAPTERS. All keyless ones are on by default; "reddit" and
        # "usajobs" need a free key each (see README "Adding coverage") and are
        # skipped with a "no API key" note until one is set, same as adzuna/reed.
        "ratemyplacement", "targetjobs", "github_internships",
        "reddit", "usajobs",
        # GradConnection serves AU/NZ/SG and skips itself elsewhere; Careerjet needs a
        # free key (CAREERJET_API_KEY); hn_hiring is Hacker News' monthly hiring thread.
        "gradconnection", "careerjet", "hn_hiring",
        # Google Jobs through SerpApi (SERPAPI_KEY; free plan is 250 searches/month).
        "google_jobs",
        # Research and science boards for engineering and computing roles: jobs.ac.uk
        # (UK universities and institutes), Science Careers and Physics Today Jobs.
        "jobsacuk", "sciencecareers", "physicstoday",
        # Off by default: brightnetwork is a confirmed dead end (Cloudflare JS
        # challenge blocks every plain request, see student_boards.py) and
        # custom_feeds does nothing until reddit_subreddits/custom_feed_urls below
        # are actually filled in, so there is no reason to enable either yet.
    ])
    companies_file: str = "data/companies_verified.yaml"
    company_limit: int | None = None
    # What the query-driven boards are asked for. Student-first by default; an empty
    # list means one unfiltered request per board.
    aggregator_queries: list[str] = field(default_factory=lambda: [
        "summer internship", "industrial placement", "graduate scheme",
    ])
    max_per_source: int = 300
    # Workday and SmartRecruiters paginate; large employers list thousands of roles.
    # Workday's page size is fixed at 20, so 400 means 20 requests per employer.
    max_per_company: int = 400
    # Per-source caps that override max_per_source. The GitHub trackers are ~25 README
    # downloads however many rows are kept, and hold ~5,000 live listings, so the
    # general cap would throw most of them away for no saving at all.
    source_limits: dict = field(default_factory=lambda: {"github_internships": 4000,
                                                          "targetjobs": 1500})
    # Add the ATS boards behind job-board apply links to the company list (harvest.py).
    harvest_boards: bool = True
    # Re-run tools/discover_startups.py at the end of a scan once the extra company list
    # is this many days old - new startups start hiring, boards move ATS. 0 = never.
    registry_refresh_days: int = 14
    # Subreddits RedditSource polls - only matters once REDDIT_CLIENT_ID/SECRET are set.
    reddit_subreddits: list[str] = field(default_factory=lambda: [
        "internships", "csMajors", "cscareerquestions",
    ])
    # Public feeds/pages CustomFeedSource fetches - empty by default, since Handshake
    # and most university career portals require a student login and forbid scraping
    # in their ToS (never touched here). If your own university, or any other board,
    # publishes a genuinely public RSS feed or listing page, add its URL here and
    # Job Scout will fetch and classify it - nothing is ever added on your behalf.
    custom_feed_urls: list[str] = field(default_factory=list)


@dataclass
class ViewPrefs:
    """The Positions sidebar's quick filters and search box - display-only, unlike
    SearchCriteria: nothing here drops or rescales a stored row, it only changes what
    the current view shows and in what order, so saving it never needs a rescore.

    Saved the same way SearchCriteria already is - per account when signed in, to the
    shared config.yaml when not (see app.py's cfg()/save_cfg()) - because before this
    existed, the sidebar reset to these same defaults on every reload, for signed-in
    and signed-out use alike.
    """
    q: str = ""
    min_score: float = 0.0
    status: str = "all"
    source: str = "all"
    order: str = "score"
    starred: bool = False
    remote: bool = False
    sponsored: bool = False
    salary_disclosed: bool = False
    unclassified: bool = False
    unviewed: bool = False          # hide postings whose detail panel you have opened
    hide_tracked: bool = True       # hide roles already on your tracker (find new ones)
    startups: str = "any"           # any | hide | only
    employment: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


@dataclass
class ScoringWeights:
    title: float = 30.0
    keywords: float = 20.0
    cv_skills: float = 25.0
    location: float = 12.0
    recency: float = 8.0
    salary: float = 5.0
    sponsorship: float = 15.0   # only applied when visa_sponsorship is not "any"


@dataclass
class Config:
    profile: Profile = field(default_factory=Profile)
    search: SearchCriteria = field(default_factory=SearchCriteria)
    sources: SourceConfig = field(default_factory=SourceConfig)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    view: ViewPrefs = field(default_factory=ViewPrefs)
    min_score: float = 25.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or os.environ.get("JOBSCOUT_CONFIG") or DEFAULT_CONFIG)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text("utf-8")) or {}
        return cls(
            profile=_build(Profile, data.get("profile")),
            search=_build(SearchCriteria, data.get("search")),
            sources=_build(SourceConfig, data.get("sources")),
            weights=_build(ScoringWeights, data.get("scoring", {}).get("weights")),
            view=_build(ViewPrefs, data.get("view")),
            min_score=float(data.get("min_score", 25.0)),
        )

    def rules(self) -> list:
        """The user's own filters, parsed and validated."""
        from .rules import parse
        return parse(self.search.custom_rules)

    def to_yaml_dict(self) -> dict:
        from dataclasses import asdict
        return {
            "profile": asdict(self.profile),
            "search": asdict(self.search),
            "sources": asdict(self.sources),
            "scoring": {"weights": asdict(self.weights)},
            "view": asdict(self.view),
            "min_score": self.min_score,
        }

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path or os.environ.get("JOBSCOUT_CONFIG") or DEFAULT_CONFIG)
        path.write_text(
            yaml.safe_dump(self.to_yaml_dict(), sort_keys=False, allow_unicode=True),
            "utf-8",
        )
        return path

    def home_country(self):
        """Resolve the target market: explicit code, else inferred from locations."""
        from . import geo
        return geo.get(self.search.country) or geo.resolve_from_locations(
            self.search.locations)

    def markets(self) -> list:
        """Every country jobs are listed for: where you are based first, then the rest."""
        from . import geo
        out: list = []
        for country in [self.home_country()] + [geo.get(m) for m in self.search.markets or []]:
            if country and country not in out:
                out.append(country)
        return out
