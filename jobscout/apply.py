"""Prepare an application so submitting it is one click, not an evening's work.

Deliberately stops short of submitting. Sending an application is irreversible and
outward-facing: it cannot be recalled, a wrong or half-tailored one burns you with that
employer, and the published reviews of tools that do submit automatically report exactly
that failure - applications fired at roles outside the user's own filters, with generated
documents that still needed editing. So Job Scout does everything up to the button and
leaves the button to you.

What it produces per job:
  * a tailored cover letter draft, built from the CV skills this posting actually asks for
  * a checklist of what the form will want, and which of those you already have
  * the real apply URL, plus anything worth knowing before you click
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import humanise
from .cv import CVProfile, display_degree, extract_skills
from .models import Job

log = logging.getLogger("jobscout.apply")

MODEL = "claude-opus-5"

# Questions most application forms ask, and what a good answer needs.
STANDARD_QUESTIONS = [
    ("Right to work / visa status",
     "Every form asks. Answer plainly; if you need sponsorship, say so."),
    ("Earliest start date", "Placements and internships almost always ask."),
    ("Why this company", "Two or three specific sentences. Generic answers read as generic."),
    ("Notice period or availability", "Usually a single date or 'immediately'."),
    ("Salary expectation", "Only if asked. A range is safer than a number."),
]


@dataclass
class ApplicationPack:
    job_id: str
    title: str
    company: str
    apply_url: str
    cover_letter: str = ""
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    checklist: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine: str = "template"

    def to_dict(self) -> dict:
        return asdict(self)


def _sentence_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _role_noun(job: Job, stage: str) -> str:
    """What to call the thing being applied for, in the applicant's own terms."""
    title = job.title.lower()
    if "placement" in title or "year in industry" in title:
        return "placement"
    if "intern" in title:
        return "internship"
    if "graduate" in title:
        return "graduate role"
    return "placement" if stage == "student" else "role"


def _template_letter(job: Job, profile: CVProfile | None, stage: str,
                     school: str, company_info: dict | None) -> str:
    """A cover letter that names real, specific things. No filler adjectives."""
    course = display_degree(profile.degrees[0]) if profile and profile.degrees else ""
    matched = job.matched_skills or []
    relevant = _sentence_list(matched[:4])
    noun = _role_noun(job, stage)

    if stage == "student":
        who = f"I am a {course} student at {school}." if school else f"I am a {course} student."
    elif stage == "graduate":
        who = (f"I am a recent {course} graduate from {school}." if school
               else f"I am a recent {course} graduate.")
    elif stage == "senior":
        who = "I am a senior engineer."
    else:
        who = "I am an engineer."

    lines = [f"Dear {job.company} hiring team,", "",
             f"I am writing to apply for the {job.title} {noun}. {who}"]

    if relevant:
        lines.append(
            f"The posting asks for {relevant}, which is where most of my hands-on "
            f"work has been. I have used {matched[0]} on coursework and personal "
            f"projects rather than only in lectures, and I can talk through what I "
            f"built and what I would do differently.")
    else:
        lines.append(
            "I have read the posting closely and believe my coursework and project "
            "work line up with what the role needs.")

    if company_info and company_info.get("description"):
        detail = company_info["description"]
        industry = ", ".join(company_info.get("industry") or [])
        lines.append(
            f"I am applying to {job.company} specifically: {detail}"
            + (f", working in {industry}" if industry else "")
            + ". [Replace this sentence with something concrete about their work "
              "that actually interests you - it is the sentence that gets read.]")
    else:
        lines.append(
            f"[Add two sentences here on why {job.company} in particular - a product, "
            f"a project, or a team you have actually looked at. This is the part "
            f"that separates your letter from every other one.]")

    lines += ["",
              "I would welcome the chance to discuss the role.", "",
              "Yours faithfully,",
              "[Your name]"]
    return "\n".join(lines)


def _claude_letter(job: Job, profile: CVProfile | None, stage: str, school: str,
                   company_info: dict | None) -> str | None:
    """Write the letter with Claude when a key is configured."""
    try:
        import anthropic
    except ImportError:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None

    skills = ", ".join((profile.skills[:12] if profile else [])) or "not supplied"
    matched = ", ".join(job.matched_skills[:8]) or "none identified"
    about = (company_info or {}).get("description", "")

    prompt = f"""Write a cover letter for this application.

Role: {job.title}
Company: {job.company}{f' - {about}' if about else ''}
Location: {job.location}
Applicant: {stage}{f' at {school}' if school else ''}
Skills on their CV: {skills}
Skills this posting asks for that they have: {matched}

Job description (truncated):
{job.description[:2500]}

Rules:
- Four short paragraphs, under 300 words total.
- Name specific skills from the overlap above. No filler adjectives, no "I am
  passionate about", no restating the job title back at them.
- Do not invent experience, employers, grades or projects the CV does not support.
- Where you cannot know something personal (why this company specifically), leave a
  bracketed instruction telling the applicant what to write there.
- Plain text, ready to paste. No markdown.

{humanise.VOICE_RULES}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("cover letter generation failed: %s", exc)
        return None

    if response.stop_reason == "refusal":
        return None
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def _voice_warnings(letter: str) -> list[str]:
    """Say which phrases read as generated, and what to put instead.

    Flagged rather than silently rewritten: half of these need a fact the app does not
    have. "Say what changed" is advice a person can act on; a quiet substitution would
    only hide the problem until a human read it.
    """
    found = humanise.tells(letter)
    if not found:
        return []
    listed = "; ".join(f"\u201c{t['phrase']}\u201d -> {t['instead']}" for t in found[:4])
    return [f"Reads as generated in {len(found)} place"
            f"{'s' if len(found) != 1 else ''}. Change: {listed}."]


def _checklist(job: Job, profile: CVProfile | None, cfg) -> list[dict]:
    have_cv = bool(profile and profile.skills)
    where = {"linkedin_export": "your LinkedIn export",
             "linkedin_pdf": "your LinkedIn profile PDF",
             "linkedin_text": "the profile text you pasted",
             "cv": "your CV"}.get(getattr(profile, "source", ""), "your profile")
    items = [
        {"item": "CV", "ready": have_cv,
         "note": (f"{len(profile.skills)} skills read from {where}" if have_cv
                  else "Nothing linked yet - import your LinkedIn on the Profile tab")},
        {"item": "Cover letter", "ready": True,
         "note": "Draft prepared below - edit the bracketed parts before sending"},
        {"item": "Apply link", "ready": bool(job.url),
         "note": job.url or "No apply URL on this posting"},
    ]
    gaps = (job.missing_skills or [])[:4]
    items.append({
        "item": "Skill gaps to address",
        "ready": not gaps,
        "note": ("Nothing obvious missing" if not gaps
                 else "Posting mentions " + ", ".join(gaps) +
                      " - be ready to say what you would do about them"),
    })
    return items


def build_pack(job: Job, profile: CVProfile | None, cfg,
               company_info: dict | None = None,
               sponsor: dict | None = None) -> ApplicationPack:
    """Everything needed to submit this application except the click."""
    stage = (getattr(cfg.search, "career_stage", "") or "student").lower()
    school = getattr(cfg.profile, "school", "") or ""

    if not job.matched_skills and profile:
        posting_skills = set(extract_skills(f"{job.title} {job.description}"))
        mine = set(profile.skills)
        job.matched_skills = sorted(posting_skills & mine)
        job.missing_skills = sorted(posting_skills - mine)[:12]

    letter = _claude_letter(job, profile, stage, school, company_info)
    engine = MODEL if letter else "template"
    if not letter:
        # No name is stored anywhere, so the draft signs off with a placeholder. That
        # is deliberate: a letter you have to put your own name on is a letter you have
        # to read first.
        letter = _template_letter(job, profile, stage, school, company_info)
    letter = humanise.soften(letter)

    warnings = []
    if not job.url:
        warnings.append("This posting has no apply URL - search the company site directly.")
    if sponsor and sponsor.get("ambiguous"):
        warnings.append(
            f"Sponsor licence matched to \"{sponsor.get('name')}\" but the name is "
            f"ambiguous - confirm it is the same company before relying on it.")
    if sponsor is None and getattr(cfg.search, "visa_sponsorship", "any") != "any":
        warnings.append("No sponsor licence found for this employer on the official "
                        "register. They may still sponsor - ask before investing time.")
    if job.closes_at:
        warnings.append(f"Closing date on the posting: {job.closes_at[:10]}")

    return ApplicationPack(
        job_id=job.id, title=job.title, company=job.company, apply_url=job.url,
        cover_letter=letter,
        matched_skills=job.matched_skills or [],
        missing_skills=(job.missing_skills or [])[:10],
        checklist=_checklist(job, profile, cfg),
        questions=[{"question": q, "note": n} for q, n in STANDARD_QUESTIONS],
        warnings=warnings + _voice_warnings(letter),
        engine=engine,
    )
