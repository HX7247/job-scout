"""People-discovery links and outreach drafts for a given posting.

DESIGN NOTE - why this module builds links instead of scraping
--------------------------------------------------------------
LinkedIn's User Agreement prohibits automated scraping of profiles, and they enforce it
technically (auth walls, rate limits, account bans). Their official APIs do not expose
people search at all. So this module does NOT fetch, store or index any LinkedIn profile.

What it does instead: construct *deep links* into LinkedIn's own search UI. Those are
just URLs. The user clicks one, LinkedIn renders it while logged in, and they see results
with their real degree-of-connection - which is strictly better than anything a scraper
could return, because it is personalised and current.

On "verified" users specifically: LinkedIn's identity-verification badge is not exposed
through any API and is not a filter you can encode in a search URL. It can only be seen
on the profile itself. The practical substitutes - and they are stronger signals for
outreach anyway - are shared school (alumni) and confirmed current employer, both of
which ARE addressable. The UI labels these honestly rather than claiming verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from .cv import CVProfile, display_degree
from .models import Job

BASE = "https://www.linkedin.com"

# Roles worth contacting, in the order a student should actually try them.
CONTACT_ROLES = [
    ("Alumni from your university", "alumni",
     "Highest reply rate. Shared-school messages get answered far more often than cold ones."),
    ("Early careers / graduate recruitment", "early_careers",
     "Owns placement and internship hiring. The right person for process questions."),
    ("Recruiters at the company", "recruiter",
     "Can flag your application internally."),
    ("People doing the job you applied for", "peer",
     "Best source of honest detail about the work itself."),
    ("Hiring managers in the team", "manager",
     "Contact last, and only with a specific, informed question."),
]

_ROLE_KEYWORDS = {
    "alumni": "",
    "early_careers": "early careers OR graduate recruitment OR campus recruiting",
    "recruiter": "recruiter OR talent acquisition",
    "peer": "",          # filled from the job title
    "manager": "",       # filled from the job title
}


def company_slug(company: str) -> str:
    """Best-guess LinkedIn company slug. Falls back to keyword search when wrong."""
    slug = re.sub(r"\b(ltd|limited|plc|inc|llc|group|holdings)\b", "", company.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


def _core_title(title: str) -> str:
    """Strip req numbers, years and level noise so a title works as a search term."""
    cleaned = re.sub(r"[\(\[].*?[\)\]]", " ", title)
    cleaned = re.sub(r"\b(20\d\d|f/m/d|m/f/d|req\s*\d+|#\d+)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(placement|internship|intern|graduate|programme|program|scheme|"
                     r"year in industry|industrial|summer|12[- ]month)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^A-Za-z0-9 &/+-]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip() or "engineer"


@dataclass
class PeopleLink:
    label: str
    role: str
    why: str
    url: str
    caveat: str = ""


def people_links(job: Job, profile: CVProfile | None = None,
                 school: str = "", school_slug: str = "",
                 home_country: str = "") -> list[PeopleLink]:
    """Deep links into LinkedIn's own search for humans attached to this posting."""
    slug = company_slug(job.company)
    core = _core_title(job.title)
    links: list[PeopleLink] = []

    if school_slug or school:
        target = school_slug or re.sub(r"[^a-z0-9]+", "-", school.lower()).strip("-")
        links.append(PeopleLink(
            label=f"Alumni of {school or target} now at {job.company}",
            role="alumni",
            why=CONTACT_ROLES[0][2],
            url=f"{BASE}/school/{target}/people/?keywords={quote_plus(job.company)}",
            caveat="If the school page 404s, use the people-search fallback below.",
        ))
        links.append(PeopleLink(
            label=f"Fallback people search: {school} + {job.company}",
            role="alumni",
            why="Works even when the school vanity URL is wrong.",
            url=f"{BASE}/search/results/people/?keywords="
                f"{quote_plus(f'{school} {job.company}')}",
        ))

    links.append(PeopleLink(
        label=f"Early careers & graduate recruitment at {job.company}",
        role="early_careers",
        why=CONTACT_ROLES[1][2],
        url=f"{BASE}/company/{slug}/people/?keywords="
            f"{quote_plus('early careers graduate recruitment')}",
        caveat="Company slug is inferred from the name; if it 404s, use the search link.",
    ))
    links.append(PeopleLink(
        label=f"Recruiters at {job.company}",
        role="recruiter",
        why=CONTACT_ROLES[2][2],
        url=f"{BASE}/search/results/people/?keywords="
            f"{quote_plus(f'{job.company} recruiter talent acquisition')}",
    ))
    links.append(PeopleLink(
        label=f"People already doing '{core}' at {job.company}",
        role="peer",
        why=CONTACT_ROLES[3][2],
        url=f"{BASE}/company/{slug}/people/?keywords={quote_plus(core)}",
    ))
    links.append(PeopleLink(
        label=f"Managers/leads for '{core}' at {job.company}",
        role="manager",
        why=CONTACT_ROLES[4][2],
        url=f"{BASE}/search/results/people/?keywords="
            f"{quote_plus(f'{job.company} {core} manager lead')}",
    ))
    links.append(PeopleLink(
        label=f"This role on LinkedIn Jobs (check for a named poster)",
        role="posting",
        why="LinkedIn often shows the hiring contact on the posting itself.",
        url=f"{BASE}/jobs/search/?keywords={quote_plus(f'{job.company} {core}')}"
            f"&location={quote_plus(job.location or home_country or 'Worldwide')}",
    ))
    return links


def published_contacts(job: Job) -> list[dict]:
    """Names/emails the posting itself published. Nothing here is scraped from LinkedIn."""
    found: list[dict] = []
    raw = job.raw or {}

    for email in (raw.get("emails") or [])[:3]:
        found.append({"kind": "email", "value": email,
                      "note": "Published in the job post itself"})

    # Greenhouse and Lever sometimes name the recruiter on the posting.
    for key in ("recruiter", "hiring_manager", "contact", "owner"):
        value = raw.get(key)
        if isinstance(value, dict):
            name = value.get("name") or " ".join(
                x for x in [value.get("first_name"), value.get("last_name")] if x)
            if name:
                found.append({"kind": "person", "value": name,
                              "note": f"Listed as {key.replace('_', ' ')} on the posting"})
        elif isinstance(value, str) and value.strip():
            found.append({"kind": "person", "value": value.strip(),
                          "note": f"Listed as {key.replace('_', ' ')} on the posting"})

    for contact in job.contacts:
        found.append({"kind": "person", "value": contact.name,
                      "note": contact.title or contact.source})
    return found


# ------------------------------------------------------------------- outreach
# Skill groups worth naming in a message. "customer service" is on the CV but says
# nothing about fit for an engineering role.
_PITCHABLE = ("engineering", "languages", "data", "cloud_devops", "web",
              "safety_quality", "finance")


def _headline_skills(profile: CVProfile | None, job: Job | None, limit: int = 2) -> str:
    """The skills worth naming: what this job actually asked for, if we know it."""
    if job is not None and job.matched_skills:
        return ", ".join(job.matched_skills[:limit])
    if not profile:
        return ""
    for group in _PITCHABLE:
        picks = profile.skills_by_group.get(group) or []
        if picks:
            return ", ".join(picks[:limit])
    return ", ".join(profile.skills[:limit])


def _self_description(profile: CVProfile | None, school: str, stage: str,
                      job: Job | None = None) -> str:
    """How the sender introduces themselves, given their career stage."""
    course = display_degree(profile.degrees[0]) if profile and profile.degrees else ""
    if stage == "student":
        where = f" at {school}" if school else ""
        return f"a {course} student{where}".replace("  ", " ").strip()
    if stage == "graduate":
        where = f" from {school}" if school else ""
        return f"a recent {course} graduate{where}".replace("  ", " ").strip()
    skills = _headline_skills(profile, job)
    lead = "a senior engineer" if stage == "senior" else "an engineer"
    return f"{lead} working in {skills}" if skills else lead


def draft_connection_note(job: Job, profile: CVProfile | None, role: str,
                          school: str = "", stage: str = "student") -> str:
    """A LinkedIn connection note. Hard limit is 300 characters."""
    core = _core_title(job.title)
    me = _self_description(profile, school, stage, job)
    # Students apply to a "placement"; everyone else applies to a "role".
    what = "placement" if stage == "student" else "role"

    if role == "alumni" and school:
        note = (f"Hi - I'm {me} and I've applied for the {core} {what} at "
                f"{job.company}. Would you be open to a quick word about your "
                f"experience there?")
    elif role == "early_careers":
        note = (f"Hi - I've applied for the {core} {what} at {job.company}. I'm {me} "
                f"and would welcome any guidance on the process.")
    elif role == "peer":
        note = (f"Hi - I'm {me}, applying for the {core} {what} at {job.company}. "
                f"Would you share what the work actually involves day to day?")
    elif role == "manager":
        note = (f"Hi - I'm {me} and I've applied for the {core} {what} on your team "
                f"at {job.company}. Would you be open to connecting?")
    else:
        note = (f"Hi - I'm {me}, applying for the {core} {what} at {job.company}. "
                f"Would you be open to connecting?")

    if len(note) > 300:
        note = note[:296].rsplit(" ", 1)[0] + "..."
    return note


def draft_message(job: Job, profile: CVProfile | None, role: str,
                  school: str = "", name: str = "", stage: str = "student") -> str:
    """A longer follow-up message, sent after the connection is accepted."""
    greeting = f"Hi {name}," if name else "Hi,"
    year = (f" (graduating {profile.graduation_year})"
            if profile and profile.graduation_year and stage in ("student", "graduate")
            else "")
    skills = ", ".join(profile.skills[:4]) if profile and profile.skills else ""
    core = _core_title(job.title)
    matched = ", ".join(job.matched_skills[:3]) if job.matched_skills else skills

    intro = f"I'm {_self_description(profile, school, stage, job)}{year}."
    body = {
        "alumni": (f"I saw you studied at {school} and are now at {job.company} - I've "
                   f"applied for the {core} placement there."),
        "early_careers": (f"I've applied for the {core} placement at {job.company} and "
                          f"wanted to introduce myself."),
        "recruiter": (f"I've applied for the {core} placement at {job.company} "
                      f"and wanted to flag my interest directly."),
        "peer": (f"I've applied for the {core} placement at {job.company} and you're "
                 f"doing close to that work already."),
        "manager": (f"I've applied for the {core} placement on your team at {job.company}."),
    }.get(role, f"I've applied for the {core} role at {job.company}.")

    ask = {
        "alumni": "Would you have ten minutes to share how you found the transition?",
        "early_careers": "Is there anything you'd suggest applicants emphasise?",
        "recruiter": "Happy to send my CV if that's useful.",
        "peer": "Would you be open to a short chat about what the role is really like?",
        "manager": "I had one specific question about the team's work, if you have a moment.",
    }.get(role, "Would you be open to a brief chat?")

    relevant = f" My background covers {matched}." if matched else ""

    return (f"{greeting}\n\n{intro} {body}{relevant}\n\n{ask}\n\n"
            f"Either way, thanks for your time.\n\nBest regards")


def contact_order(stage: str) -> list[tuple[str, str, str]]:
    """Who to approach first depends on where you are in your career.

    A student's best route is a shared-school alumnus. An experienced hire's is the
    recruiter or the hiring manager - university ties are years stale, and early
    careers teams do not handle experienced roles at all.
    """
    if stage in ("student", "graduate"):
        return CONTACT_ROLES
    preferred = ["recruiter", "peer", "manager", "alumni", "early_careers"]
    by_role = {entry[1]: entry for entry in CONTACT_ROLES}
    return [by_role[r] for r in preferred if r in by_role]


def outreach_pack(job: Job, profile: CVProfile | None = None, school: str = "",
                  school_slug: str = "", home_country: str = "",
                  stage: str = "student") -> dict:
    """Everything the UI needs for the 'who can I talk to' panel of one job."""
    roles = contact_order(stage)
    order = [role for _, role, _ in roles]
    links = people_links(job, profile, school, school_slug, home_country)
    links.sort(key=lambda l: order.index(l.role) if l.role in order else len(order))
    return {
        "links": [vars(l) for l in links],
        "published_contacts": published_contacts(job),
        "contact_order": order,
        "drafts": {
            role: {
                "connection_note": draft_connection_note(job, profile, role, school, stage),
                "message": draft_message(job, profile, role, school, stage=stage),
            }
            for _, role, _ in roles
        },
        "disclosure": (
            "These open LinkedIn's own search - no profile data is scraped or stored. "
            "LinkedIn does not expose its verification badge to any API or search filter, "
            "so results cannot be filtered by verified status; shared school and current "
            "employer are shown instead, which are better outreach signals anyway."
        ),
    }
