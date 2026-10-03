"""Source adapters. Each returns list[Job]; failures degrade to an empty list."""
from .ats import (
    GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource,
    WorkableSource, RecruiteeSource, PersonioSource, WorkdaySource,
    ATS_ADAPTERS, discover_ats,
)
from .aggregators import (
    AdzunaSource, ReedSource, ArbeitnowSource, RemotiveSource, RemoteOKSource,
    JobicySource, HimalayasSource, TheMuseSource, HNHiringSource,
    AGGREGATOR_ADAPTERS,
)
from .student_boards import (
    GradcrackerSource, RateMyPlacementSource, BrightNetworkSource,
    STUDENT_BOARD_ADAPTERS,
)
from .reddit import RedditSource
from .github_boards import GitHubInternshipsSource
from .usajobs import USAJobsSource
from .customfeeds import CustomFeedSource
from .grad_boards import GradConnectionSource, CareerjetSource, GRAD_BOARD_ADAPTERS
from .google_jobs import GoogleJobsSource

# The community/forum/student-board tier: same BaseAggregator contract as
# AGGREGATOR_ADAPTERS, kept in its own dict only because they were built and are
# documented as a distinct wave of coverage (see README "Adding coverage").
COMMUNITY_ADAPTERS = {
    **STUDENT_BOARD_ADAPTERS,
    **GRAD_BOARD_ADAPTERS,
    GoogleJobsSource.name: GoogleJobsSource(),
    RedditSource.name: RedditSource(),
    GitHubInternshipsSource.name: GitHubInternshipsSource(),
    USAJobsSource.name: USAJobsSource(),
    CustomFeedSource.name: CustomFeedSource(),
}

ALL_ADAPTERS = {**ATS_ADAPTERS, **AGGREGATOR_ADAPTERS, **COMMUNITY_ADAPTERS}

__all__ = [
    "ALL_ADAPTERS", "ATS_ADAPTERS", "AGGREGATOR_ADAPTERS", "COMMUNITY_ADAPTERS",
    "discover_ats",
]
