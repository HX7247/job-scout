"""Build a matching profile from the user's own LinkedIn, without asking them anything.

WHY IT WORKS THIS WAY - read before "improving" it into a scraper
-----------------------------------------------------------------
Pasting a profile URL cannot fetch a profile. LinkedIn auth-walls profile pages, and
their User Agreement forbids automated access; the hiQ litigation ended in 2022 with
LinkedIn winning on breach of contract, so "it is public data" is not a defence. Their
official APIs do not help either: Sign In with LinkedIn (OpenID Connect) returns name,
email and photo - more personal data than we want and none of the experience, skills or
education we actually need. Partner access to the full profile API is not open to
individuals.

So the app uses the one route that is both permitted and complete: **LinkedIn's own data
export**, which LinkedIn gives the user on request, and the profile PDF LinkedIn
generates from the "Save to PDF" button. Both are the user's own copy of their own data,
handed over by LinkedIn, parsed here on this machine. That is strictly richer than any
API would give us, and it involves no scraping at all.

WHAT IS KEPT
------------
The export contains a lot of personal data - email addresses, phone numbers, postal
address, date of birth, the full connection list, messages. This module reads the four
files it needs, derives the matching signal, and never writes the rest anywhere. The
allowlist in privacy.py is what actually enforces that; see ``derive``.

Accepted input:
  * Basic_LinkedInDataExport_*.zip   (Settings -> Data privacy -> Get a copy of your data)
  * Profile.pdf                     (your profile page -> More -> Save to PDF)
  * pasted profile text             (select-all on your own profile, for the impatient)
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

from . import privacy
from .cv import ALL_SKILLS, SKILL_GROUP, extract_keywords, extract_skills

log = logging.getLogger("jobscout.linkedin_import")

# ------------------------------------------------------------------ education levels
# Ordered weakest to strongest; the highest match wins.
LEVELS = [
    ("secondary", ["gcse", "a-level", "a level", "a-levels", "highers", "baccalaureat",
                   "abitur", "high school", "ib diploma", "btec level 3"]),
    ("foundation", ["foundation", "access to he", "hnc", "level 4"]),
    ("diploma", ["hnd", "diploma", "associate", "foundation degree", "level 5",
                 "apprenticeship", "nvq"]),
    ("bachelors", ["bachelor", "beng", "bsc", "ba ", "b.a", "b.sc", "b.eng", "btech",
                   "b.tech", "bba", "llb", "undergraduate", "licence", "laurea triennale"]),
    ("masters", ["master", "meng", "msc", "ma ", "m.a", "m.sc", "m.eng", "mtech",
                 "m.tech", "mba", "llm", "mphil", "postgraduate", "mres", "diplom"]),
    ("doctorate", ["phd", "ph.d", "dphil", "doctorate", "doctoral", "md", "engd"]),
]

# Abbreviation to print back to the user, longest and most specific first.
DEGREE_CODES = [
    ("EngD", r"\bengd\b"), ("PhD", r"\b(?:phd|ph\.?d|dphil|doctorate)\b"),
    ("MEng", r"\bm\.?eng\b"), ("MSci", r"\bmsci\b"), ("MSc", r"\bm\.?sc\b"),
    ("MBA", r"\bmba\b"), ("MPhil", r"\bmphil\b"), ("MRes", r"\bmres\b"),
    ("LLM", r"\bllm\b"), ("M.Tech", r"\bm\.?tech\b"), ("MA", r"\bm\.?a\b"),
    ("BEng", r"\bb\.?eng\b"), ("BSc", r"\bb\.?sc\b"), ("BBA", r"\bbba\b"),
    ("LLB", r"\bllb\b"), ("B.Tech", r"\bb\.?tech\b"), ("BA", r"\bb\.?a\b"),
    ("HND", r"\bhnd\b"), ("HNC", r"\bhnc\b"), ("BTEC", r"\bbtec\b"),
    ("Master's", r"\bmaster'?s?\b"), ("Bachelor's", r"\bbachelor'?s?\b"),
    ("A-Levels", r"\ba[- ]levels?\b"),
]

# Words that are the qualification, not the subject. Stripped to leave the field of study.
_DEGREE_WORDS = re.compile(
    r"\b(?:bachelor'?s?|master'?s?|doctor(?:ate)?|of|science|arts|engineering\s+degree|"
    r"beng|bsc|ba|meng|msc|ma|mba|mphil|mres|msci|phd|dphil|engd|llb|llm|btech|mtech|"
    r"hnd|hnc|btec|bba|with\s+honours?|hons?|degree|undergraduate|postgraduate)\b",
    re.I)

SENIOR_WORDS = re.compile(
    r"\b(senior|lead|principal|staff|head|director|chief|manager|vp|architect)\b", re.I)
JUNIOR_WORDS = re.compile(
    r"\b(intern|internship|placement|trainee|apprentice|graduate|junior|assistant|"
    r"student|volunteer|work experience)\b", re.I)


# ------------------------------------------------------------------------- reading
def _rows(text: str) -> list[dict]:
    """Parse one CSV with tolerant headers - LinkedIn renames columns without notice."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        return [{(k or "").strip().lower(): (v or "").strip()
                 for k, v in row.items()} for row in reader]
    except Exception as exc:
        log.warning("could not parse a CSV from the export: %s", exc)
        return []


def _pick(row: dict, *names: str) -> str:
    """First present column among several spellings."""
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


# Files worth opening. Everything else in the archive is ignored by name, so a future
# addition to the export cannot quietly become something we read.
WANTED = {
    "profile": ("profile.csv",),
    "positions": ("positions.csv",),
    "education": ("education.csv",),
    "skills": ("skills.csv",),
    "languages": ("languages.csv",),
    "certifications": ("certifications.csv",),
    "projects": ("projects.csv",),
    # Not one of LinkedIn's 7 official profile-strength sections (see profile_review) -
    # read only as a bonus signal for that review. Kept out of anything persisted, same
    # as every other table here: derive() never reads this key, so a recommendation's
    # text or the recommender's name cannot reach privacy.clean_profile_dict at all.
    "recommendations": ("recommendations_received.csv",),
}


def read_export(path: str | Path) -> dict[str, list[dict]]:
    """Open a LinkedIn export zip and return only the tables we use."""
    tables: dict[str, list[dict]] = {}
    with zipfile.ZipFile(path) as archive:
        names = {Path(n).name.lower(): n for n in archive.namelist()}
        for key, candidates in WANTED.items():
            for candidate in candidates:
                if candidate in names:
                    raw = archive.read(names[candidate]).decode("utf-8-sig", "ignore")
                    tables[key] = _rows(raw)
                    break
    if not tables:
        raise ValueError(
            "That zip does not look like a LinkedIn export - no Profile.csv inside. "
            "Request it under Settings > Data privacy > Get a copy of your data, and "
            "pick the larger archive rather than the connections-only one.")
    return tables


# --------------------------------------------------------------------- derivations
def _months(started: str, finished: str) -> int:
    """Length of one position in whole months. LinkedIn writes 'Mar 2024' or '2024'."""
    def parse(value: str) -> date | None:
        value = (value or "").strip()
        if not value:
            return None
        for fmt in ("%b %Y", "%B %Y", "%Y-%m-%d", "%Y-%m", "%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    start = parse(started)
    if not start:
        return 0
    end = parse(finished) or date.today()
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def _merged_experience_months(positions: list[dict]) -> int:
    """Total months worked, counting overlapping roles once.

    Two part-time jobs held the same summer is three months of experience, not six.
    """
    spans = []
    for row in positions:
        started = _pick(row, "started on", "start date", "started")
        finished = _pick(row, "finished on", "end date", "finished")
        length = _months(started, finished)
        if length:
            # Store as (start_index, end_index) in months since year 0 for easy merging.
            for fmt in ("%b %Y", "%B %Y", "%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    begin = datetime.strptime(started.strip(), fmt).date()
                    spans.append((begin.year * 12 + begin.month,
                                  begin.year * 12 + begin.month + length))
                    break
                except ValueError:
                    continue
    if not spans:
        return 0
    spans.sort()
    total, current_start, current_end = 0, *spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + (current_end - current_start)


def education_level(text: str) -> str:
    """Highest qualification named anywhere in the education text."""
    lower = f" {(text or '').lower()} "
    best = ""
    for level, signals in LEVELS:
        if any(signal in lower for signal in signals):
            best = level
    return best


def degree_code(text: str) -> str:
    """How the qualification should be written back to the user - 'MEng', not 'Master'."""
    lower = (text or "").lower()
    for label, pattern in DEGREE_CODES:
        if re.search(pattern, lower):
            return label
    return ""


def field_of_study(degree_name: str, notes: str = "") -> str:
    """The subject, with the qualification words taken out.

    LinkedIn stores it as one free-text field: "Bachelor of Engineering - BEng,
    Mechanical Engineering". Everything before the subject is boilerplate.
    """
    text = degree_name or notes or ""
    # The subject is usually the last comma- or dash-separated clause.
    parts = [p.strip() for p in re.split(r"[,;]|\s[-–]\s", text) if p.strip()]
    for part in reversed(parts):
        cleaned = _DEGREE_WORDS.sub(" ", part)
        cleaned = re.sub(r"[^A-Za-z&/ ]", " ", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if len(cleaned) > 3:
            return cleaned.title()
    return ""


def _career_stage(level: str, months: int, titles: list[str],
                  studying_now: bool) -> str:
    """Which stage to search at. Being mid-degree beats any amount of summer work."""
    if studying_now:
        return "student"
    senior = sum(1 for t in titles if SENIOR_WORDS.search(t))
    if senior >= 2 or months >= 96:
        return "senior"
    if months >= 30:
        return "professional"
    if level in ("bachelors", "masters", "doctorate") and months < 30:
        return "graduate"
    return "student"


def _skill_names(tables: dict) -> list[str]:
    """Skills as LinkedIn holds them, plus anything recognisable in the descriptions."""
    listed = [_pick(row, "name", "skill") for row in tables.get("skills", [])]
    listed = [s for s in listed if s]

    # Position and project descriptions are where the tools actually get named.
    prose = " ".join(
        _pick(row, "description", "title") for key in ("positions", "projects")
        for row in tables.get(key, []))
    found = extract_skills(prose) if prose.strip() else []

    known, custom, seen = [], [], set()
    for skill in listed + found:
        key = skill.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        (known if key in ALL_SKILLS else custom).append(key)
    return known + custom


def derive(tables: dict[str, list[dict]], source: str = "linkedin_export") -> dict:
    """Turn the export tables into the profile we keep. Drops everything else.

    The return value goes through ``privacy.clean_profile_dict`` on the way out, so a
    field added here without being allowlisted is discarded rather than stored.
    """
    profile_row = (tables.get("profile") or [{}])[0]
    educations = tables.get("education") or []
    positions = tables.get("positions") or []

    # --- education: the highest level across every entry, with its own degree/subject
    best_level, best_entry = "", {}
    order = [name for name, _ in LEVELS]
    for row in educations:
        text = " ".join([_pick(row, "degree name", "degree"),
                         _pick(row, "notes"), _pick(row, "activities")])
        level = education_level(text)
        if level and (not best_level or order.index(level) > order.index(best_level)):
            best_level, best_entry = level, row
    if not best_entry and educations:
        best_entry = educations[0]

    degree_text = _pick(best_entry, "degree name", "degree")
    end_dates = [_pick(r, "end date", "finished on") for r in educations]
    years = [m.group(0) for d in end_dates if (m := re.search(r"20\d\d|19\d\d", d or ""))]

    def unfinished(row: dict) -> bool:
        """Still studying: no end date, or one in the current year or later."""
        ends = _pick(row, "end date", "finished on")
        if not ends:
            return True
        match = re.search(r"(19|20)\d\d", ends)
        return bool(match) and int(match.group(0)) >= date.today().year

    studying_now = any(unfinished(row) for row in educations)

    # --- experience
    titles = [_pick(row, "title", "position") for row in positions]
    titles = [t for t in titles if t]
    months = _merged_experience_months(positions)

    # Most recent first. LinkedIn writes the export newest-first already, but an export
    # edited by hand or produced by a different locale may not, so sort explicitly.
    def start_key(row: dict) -> str:
        raw = _pick(row, "started on", "start date")
        match = re.search(r"(19|20)\d\d", raw)
        return match.group(0) if match else "0000"
    recent = [_pick(row, "title", "position")
              for row in sorted(positions, key=start_key, reverse=True)][:6]

    # --- fields of work actually worked in, via the app's own classifier
    from .classify import classify
    families, seen = [], set()
    for row in positions:
        _, family = classify(_pick(row, "title", "position"),
                             _pick(row, "description"), "", "")
        if family and family not in seen:
            seen.add(family)
            families.append(family)

    skills = _skill_names(tables)
    by_group: dict[str, list[str]] = {}
    for skill in skills:
        by_group.setdefault(SKILL_GROUP.get(skill, "other"), []).append(skill)

    industry = _pick(profile_row, "industry")
    derived = {
        "source": source,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "education_level": best_level,
        "degree": degree_code(degree_text),
        "field_of_study": field_of_study(degree_text, _pick(best_entry, "notes")),
        "institution": _pick(best_entry, "school name", "school"),
        "graduation_year": max(years) if years else "",
        "degrees": [degree_code(_pick(r, "degree name", "degree")) or "" for r in educations],
        "skills": skills,
        "skills_by_group": by_group,
        "custom_skills": [s for s in skills if s not in ALL_SKILLS],
        "keywords": extract_keywords(" ".join(
            [_pick(profile_row, "headline"), _pick(profile_row, "summary")]
            + [_pick(r, "description") for r in positions])),
        "years_experience": round(months / 12, 1),
        "recent_titles": recent,
        # Two different things, kept apart on purpose: "industries" is what LinkedIn
        # and the user call it, "families" are this app's own filter slugs. Mixed
        # together, an industry label could be written into search.job_families as a
        # filter no posting can ever match.
        "industries": ([industry] if industry else []),
        "families": families,
        "languages": [_pick(r, "name", "language") for r in tables.get("languages", [])
                      if _pick(r, "name", "language")],
        # Coarse on purpose. LinkedIn's Geo Location is city-level and public; the
        # postal address in the same file is not, and is never read.
        "region": _pick(profile_row, "geo location", "location"),
        "headline": _pick(profile_row, "headline"),
    }
    derived["degrees"] = [d for d in derived["degrees"] if d]
    clean = privacy.clean_profile_dict(derived)
    clean["career_stage"] = _career_stage(best_level, months, titles, studying_now)
    return clean


# ------------------------------------------------------------------- PDF / text path
_PDF_SECTIONS = re.compile(
    r"^\s*(experience|education|skills|licenses|certifications|languages|summary|"
    r"top skills|honors|projects|publications)\s*$", re.I | re.M)


def derive_from_text(text: str, source: str = "linkedin_pdf") -> dict:
    """Fallback for the profile PDF or pasted profile text.

    Section-header splitting, because LinkedIn's PDF is a flat text dump with the same
    headings every time. Less precise than the CSV export - no dates means no experience
    total - so the UI recommends the export and offers this second.
    """
    text = privacy.scrub(text)
    sections: dict[str, str] = {}
    matches = list(_PDF_SECTIONS.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = match.group(1).lower().replace("top skills", "skills")
        sections[name] = sections.get(name, "") + "\n" + text[match.end():end]

    education_text = sections.get("education", "")
    skills_text = sections.get("skills", "")
    experience_text = sections.get("experience", "")

    listed = [line.strip(" •-\t") for line in skills_text.splitlines()
              if 2 < len(line.strip()) < 44]
    known, custom, seen = [], [], set()
    for skill in listed + extract_skills(f"{skills_text} {experience_text}"):
        key = skill.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        (known if key in ALL_SKILLS else custom).append(key)
    skills = known + custom

    by_group: dict[str, list[str]] = {}
    for skill in skills:
        by_group.setdefault(SKILL_GROUP.get(skill, "other"), []).append(skill)

    level = education_level(education_text) or education_level(text)
    years = re.findall(r"\b(20[2-4]\d)\b", education_text)
    # In a PDF the institution is the line above the qualification.
    institution = ""
    lines = [l.strip() for l in education_text.splitlines() if l.strip()]
    for index, line in enumerate(lines):
        if degree_code(line) and index:
            institution = lines[index - 1]
            break
    else:
        institution = lines[0] if lines else ""

    titles = [l.strip() for l in experience_text.splitlines()
              if 4 < len(l.strip()) < 70
              and (JUNIOR_WORDS.search(l) or SENIOR_WORDS.search(l))]

    derived = {
        "source": source,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "education_level": level,
        "degree": degree_code(education_text),
        "field_of_study": field_of_study(
            next((l for l in lines if degree_code(l)), ""), education_text),
        "institution": institution,
        "graduation_year": max(years) if years else "",
        "skills": skills,
        "skills_by_group": by_group,
        "custom_skills": custom,
        "keywords": extract_keywords(text),
        "recent_titles": titles[:6],
        "headline": (lines[0] if lines else ""),
        "years_experience": 0.0,
        "industries": [],
        "languages": [],
        "region": "",
    }
    clean = privacy.clean_profile_dict(derived)
    future = [y for y in years if int(y) >= date.today().year]
    clean["career_stage"] = _career_stage(level, 0, titles, bool(future))
    return clean


def from_file(path: str | Path) -> dict:
    """Import whichever of the two LinkedIn exports the user actually has."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".zip":
        return derive(read_export(path), source="linkedin_export")
    if path.suffix.lower() in (".pdf", ".txt", ".docx"):
        from .cv import read_document
        return derive_from_text(read_document(path),
                                source="linkedin_pdf" if path.suffix.lower() == ".pdf"
                                else "linkedin_text")
    raise ValueError(f"unsupported file: {path.suffix}. Use the export .zip or the "
                     f"profile .pdf.")


# --------------------------------------------------------------- profile review
# LinkedIn's own profile-strength meter, confirmed against LinkedIn's help page on
# 2026-09-24: linkedin.com/help/linkedin/answer/a594698. It names exactly these seven
# sections, and states plainly that reaching four of them gets to "Intermediate" and all
# seven gets to "All-Star" - a completion checklist, not a hidden algorithmic score. That
# page is the source for every "why" sentence below that describes LinkedIn's own
# behaviour; nothing here repeats the "40x more views"-style numbers that third-party
# blogs attribute to LinkedIn without LinkedIn ever having said them.
_OFFICIAL_SECTIONS = [
    "Profile photo", "Location", "Industry", "Education",
    "Position (work experience)", "Skills", "Summary",
]


def _section(name: str, present: bool | None, why: str) -> dict:
    """One row of the 7-section checklist. ``present=None`` means genuinely unknown -
    not a guess, and not silently counted as either present or missing."""
    return {"name": name, "present": present, "why": why}


def profile_review(profile: dict, tables: dict[str, list[dict]] | None = None) -> dict:
    """Score how complete the SIGNED-IN user's own LinkedIn profile is, with tips.

    Two clearly separate things come back, and the caller should keep them visually
    separate too:

      "official"     - the 7 sections LinkedIn's own profile-strength meter checks
                        (see the module note above). Presence is read from ``profile``,
                        the already-derived, allowlisted dict - never from anything that
                        would have to be invented.
      "quality_tips" - Job Scout's own extra checks, using whatever raw signal is on
                        hand (skills count, whether a role has a description, whether an
                        education entry has an end date, languages/certifications/
                        projects as bonus signals). These are NOT LinkedIn claims and are
                        labelled as such throughout.

    ``tables`` is the raw dict from ``read_export`` - the actual CSV rows, including
    free text like the Summary field and position descriptions. Passing it in gives a
    fuller review (it can tell whether a Summary exists, and whether roles have
    descriptions) but that text is used here and only here, in memory, for the length of
    this one call - it is never returned, never stored, and this function does not
    write it anywhere. ``tables=None`` is the honest default for the read path this app
    actually has: raw export tables are not kept anywhere after import (see
    api_profile_import in app.py), so most calls only have the persisted ``profile``
    dict to work with, and this function says so plainly in the "note" it returns rather
    than pretending the two cases give the same fidelity.

    A profile derived from the PDF/pasted-text fallback (``source`` is "linkedin_pdf" or
    "linkedin_text") never has a structured location or industry field at all -
    ``derive_from_text`` has no section for either, so those two checks are reported as
    unknown rather than "missing", which would be a claim about the profile this
    function has no basis for. Nothing here fetches or re-reads anything from LinkedIn;
    it only reads what ``derive``/``derive_from_text`` already produced or what
    ``tables`` already holds in memory.
    """
    profile = profile or {}
    source = profile.get("source", "")
    from_pdf = source in ("linkedin_pdf", "linkedin_text")
    profile_row = (tables.get("profile") or [{}])[0] if tables else {}

    pdf_caveat = (" A flat PDF/text export has no separate field for this, so it can't "
                  "be read out the way it can from the data export - check your live "
                  "profile if you want to be sure.")

    sections = [
        _section("Profile photo", None,
                  "Recruiters, connections and search results see this at the top of "
                  "every profile. Job Scout never opens image data from your export or "
                  "PDF (see WANTED above), so this can never be checked here - confirm "
                  "it yourself on linkedin.com."),
    ]

    if from_pdf:
        sections.append(_section(
            "Location", None,
            "Location-based search reads this field directly." + pdf_caveat))
        sections.append(_section(
            "Industry", None,
            "The industry tag is one of the filters recruiters can search by, and it "
            "shapes which suggested jobs LinkedIn shows you." + pdf_caveat))
    else:
        sections.append(_section(
            "Location", bool(profile.get("region")),
            "Location-based search and \"jobs near you\" matching read this field "
            "directly; without it your profile does not surface for a geographic "
            "search."))
        sections.append(_section(
            "Industry", bool(profile.get("industries")),
            "The industry tag is one of the filters recruiters can search by, and it "
            "shapes which suggested jobs and groups LinkedIn shows you."))

    sections.append(_section(
        "Education", bool(profile.get("education_level") or profile.get("institution")),
        "Employers and alumni recruiters commonly search by school and degree; with "
        "nothing in this section they have nothing to match on."))
    sections.append(_section(
        "Position (work experience)",
        bool(profile.get("recent_titles") or profile.get("years_experience")),
        "This is usually the first section a recruiter reads to judge relevant "
        "experience; an empty one gives them nothing to evaluate."))
    sections.append(_section(
        "Skills", bool(profile.get("skills")),
        "LinkedIn's own search and skill-match features index against this list; with "
        "none listed, your profile cannot surface for a skill-based search."))

    if tables is not None:
        summary_text = _pick(profile_row, "summary")
        sections.append(_section(
            "Summary", bool(summary_text.strip()),
            "The About/summary section is the one place to describe yourself in your "
            "own words rather than through job titles and dates - often the first "
            "thing read after your headline."))
    else:
        sections.append(_section(
            "Summary", None,
            "The About/summary section is the one place to describe yourself in your "
            "own words rather than through job titles and dates - often the first "
            "thing read after your headline. Job Scout never stores this text (only "
            "counts and booleans are kept, see privacy.PROFILE_ALLOWLIST), so this "
            "review cannot tell whether you have one without re-reading your export."))

    known = [s for s in sections if s["present"] is not None]
    present = [s["name"] for s in known if s["present"]]
    missing = [s["name"] for s in known if not s["present"]]
    unknown = [s["name"] for s in sections if s["present"] is None]

    official = {
        "sections": sections,
        "total": len(_OFFICIAL_SECTIONS),
        "present_count": len(present),
        "checked_count": len(known),
        "missing": missing,
        "unknown": unknown,
        "summary_line": (
            f"{len(present)} of {len(_OFFICIAL_SECTIONS)} core sections present"
            + (f" - missing: {', '.join(missing)}" if missing else "")
            + (f" ({len(unknown)} can't be checked here: {', '.join(unknown)})"
               if unknown else "")),
        "source": "LinkedIn Help: linkedin.com/help/linkedin/answer/a594698 - the same "
                  "seven sections LinkedIn's own profile-strength meter (Beginner at 0, "
                  "Intermediate at 4, All-Star at all 7) checks. It is a completion "
                  "checklist, not an algorithmic score, and LinkedIn states there is no "
                  "hidden ranking beyond it.",
    }

    # ---- Job Scout's own quality checks - separate from the official list above,
    # and never dressed up as something LinkedIn said.
    tips = []
    if from_pdf:
        tips.append({
            "label": "Import source",
            "detail": "Read from a PDF or pasted text, not the full data export. "
                      "Per-role descriptions and your summary text are not recoverable "
                      "from that format, so several checks below are skipped. Request "
                      "the full export (Settings & Privacy > Data privacy > Get a copy "
                      "of your data) for a complete review.",
        })

    skills = profile.get("skills") or []
    tips.append({
        "label": "Skills listed",
        "detail": (f"{len(skills)} skill(s) listed. More listed skills give recruiter "
                   "search and LinkedIn's own skill-match features more to match "
                   "against." if skills else
                   "No skills listed. With none, the Skills section will not even "
                   "appear on your profile."),
    })

    languages = profile.get("languages") or []
    if languages:
        tips.append({"label": "Languages",
                     "detail": f"{len(languages)} language(s) listed - a bonus signal, "
                               "not one of LinkedIn's 7 core sections."})

    if tables is not None:
        positions = tables.get("positions") or []
        if positions:
            described = sum(1 for row in positions if _pick(row, "description").strip())
            if described < len(positions):
                tips.append({
                    "label": "Position descriptions",
                    "detail": f"{len(positions) - described} of {len(positions)} "
                              "listed role(s) have no description - directly visible "
                              "in your export, nothing to do with LinkedIn's own "
                              "checklist. A role with no description shows only a "
                              "title and dates, nothing about what you actually did.",
                })

        educations = tables.get("education") or []
        unfinished = sum(1 for row in educations
                         if not _pick(row, "end date", "finished on").strip())
        if educations and unfinished:
            tips.append({
                "label": "Education end dates",
                "detail": f"{unfinished} of {len(educations)} education entries have "
                          "no end date. Expected while still studying - worth checking "
                          "if a listed course has actually finished.",
            })

        certifications = tables.get("certifications") or []
        projects = tables.get("projects") or []
        if certifications or projects:
            tips.append({
                "label": "Certifications / projects (bonus, not one of the 7 core "
                         "sections)",
                "detail": f"{len(certifications)} certification(s) and {len(projects)} "
                          "project(s) on file - both give recruiters something "
                          "concrete beyond a job title to look at.",
            })

        recommendations = tables.get("recommendations")
        if recommendations is not None:
            tips.append({
                "label": "Recommendations received (bonus, not one of the 7 core "
                         "sections)",
                "detail": (f"{len(recommendations)} recommendation(s) on file."
                          if recommendations else
                          "None on file. A written recommendation is harder to fake "
                          "than a skill endorsement."),
            })
    else:
        tips.append({
            "label": "Deeper checks unavailable",
            "detail": "This review used only your saved profile (education, skills, "
                      "titles, region, etc), not the raw export text - Job Scout never "
                      "stores that. Re-import your export .zip on this tab to also "
                      "check your summary and whether each role has a description.",
        })

    return {
        "official": official,
        "quality_tips": tips,
        "from_pdf_or_text": from_pdf,
        "note": ("Computed from your saved profile only. Raw export text such as your "
                 "summary or per-role descriptions is never stored, so those two "
                 "checks are skipped here - see the 'Deeper checks unavailable' tip."
                 if tables is None else
                 "Computed from your export, read fresh for this review and not "
                 "stored anywhere."),
    }


# ---------------------------------------------------------------- what to search for
# Which job titles a given subject plausibly leads to. Deliberately short lists: these
# are suggestions shown for approval, not a taxonomy, and a wrong guess is visible.
SUBJECT_TITLES = {
    "mechanical": ["mechanical engineer", "design engineer", "manufacturing engineer",
                   "cad engineer", "graduate engineer"],
    "biomedical": ["biomedical engineer", "medical device engineer", "clinical engineer",
                   "regulatory affairs", "quality engineer"],
    "electrical": ["electrical engineer", "electronics engineer", "controls engineer",
                   "power systems engineer"],
    "civil": ["civil engineer", "structural engineer", "site engineer",
              "geotechnical engineer"],
    "chemical": ["chemical engineer", "process engineer", "energy engineer"],
    "aerospace": ["aerospace engineer", "systems engineer", "stress engineer"],
    "computer": ["software engineer", "software developer", "backend engineer",
                 "data engineer"],
    "software": ["software engineer", "backend engineer", "frontend engineer",
                 "full stack developer"],
    "data": ["data analyst", "data scientist", "data engineer", "analyst"],
    "math": ["data analyst", "quantitative analyst", "actuarial analyst"],
    "statistics": ["data scientist", "statistician", "data analyst"],
    "physics": ["data analyst", "research engineer", "systems engineer"],
    "econom": ["economist", "analyst", "data analyst", "research analyst"],
    "finance": ["finance analyst", "investment analyst", "financial analyst"],
    "account": ["audit associate", "accountant", "finance analyst"],
    "business": ["business analyst", "operations analyst", "project coordinator"],
    "marketing": ["marketing executive", "digital marketing assistant", "brand assistant"],
    "law": ["paralegal", "legal assistant", "trainee solicitor"],
    "psycholog": ["people analyst", "hr assistant", "research assistant"],
    "nurs": ["staff nurse", "healthcare assistant"],
    "chemistry": ["laboratory technician", "analytical chemist", "process chemist"],
    "biolog": ["laboratory technician", "research assistant", "quality technician"],
    "architect": ["architectural assistant", "part 1 architectural assistant"],
    "environment": ["environmental consultant", "sustainability analyst"],
}

# The same subject keywords, mapped onto classify.py's job_family keys instead of job
# titles - used to work out which fields of work to look up in "companies hiring in
# your field" (see target_families below and Store.top_employers). Not every subject
# above needs an entry here; one that is missing just means that panel stays quiet for
# it rather than guessing.
SUBJECT_FAMILIES = {
    "mechanical": ["engineering"], "biomedical": ["engineering", "science"],
    "electrical": ["engineering"], "civil": ["engineering", "construction"],
    "chemical": ["engineering", "science"], "aerospace": ["engineering"],
    "computer": ["software"], "software": ["software"], "data": ["data"],
    "math": ["data", "finance"], "statistics": ["data"], "physics": ["engineering", "data"],
    "econom": ["finance", "data"], "finance": ["finance"], "account": ["finance"],
    "business": ["operations", "sales"], "marketing": ["marketing"], "law": ["legal"],
    "psycholog": ["hr", "data"], "nurs": ["healthcare"], "chemistry": ["science"],
    "biolog": ["science"], "architect": ["construction", "creative"],
    "environment": ["science", "operations"],
}

# What a given stage should actually be searching for, appended to the subject titles.
STAGE_TITLES = {
    "student": ["industrial placement", "year in industry", "summer internship",
                "placement student", "intern"],
    "graduate": ["graduate scheme", "graduate engineer", "graduate analyst",
                 "junior", "trainee"],
    "professional": [],
    "senior": [],
}


def suggestions(profile: dict) -> dict:
    """Search criteria inferred from the profile, for the user to accept or edit.

    Nothing here is applied automatically. The point of the import is to save typing,
    not to take decisions - a wrong inference the user cannot see would be worse than
    the empty form it replaced.
    """
    from . import geo

    stage = profile.get("career_stage") or "student"
    subject = (profile.get("field_of_study") or "").lower()
    headline = (profile.get("headline") or "").lower()

    titles: list[str] = []
    for key, options in SUBJECT_TITLES.items():
        if key in subject or key in headline:
            titles.extend(options)
    titles.extend(STAGE_TITLES.get(stage, []))
    # Past job titles are the strongest signal of all for anyone with a work history.
    for title in profile.get("recent_titles", [])[:3]:
        cleaned = re.sub(r"\s*[-–(].*$", "", title).strip().lower()
        if 3 < len(cleaned) < 40 and not JUNIOR_WORDS.search(cleaned):
            titles.append(cleaned)

    ordered, seen = [], set()
    for title in titles:
        if title and title not in seen:
            seen.add(title)
            ordered.append(title)

    region = profile.get("region") or ""
    country = geo.primary_country(region) if region else None
    city = region.split(",")[0].strip() if region else ""

    return {
        "career_stage": stage,
        "titles": ordered[:14],
        "keywords_any": sorted(set(profile.get("skills", [])[:18])),
        # NOT a family filter. The families in the profile are where the person has
        # WORKED - a biomedical engineering student who had a Costa job and a Boots job
        # would get "hospitality, retail" proposed as their target field, and ticking it
        # hides every engineering placement they are actually looking for. Tested: it
        # dropped 1,038 of 1,833 postings and left nothing. Shown for information only.
        "worked_in": [f for f in profile.get("families", []) if f][:5],
        "job_families": [],
        "locations": [city] if city else [],
        "country": country or "",
        "exclude_senior": stage in ("student", "graduate"),
        "why": _explain(profile, stage, ordered[:14]),
    }


def target_families(profile: dict) -> list[str]:
    """Which classify.py job_family keys this profile's subject plausibly points at.

    Used only for the informational "who's hiring in your field" panel, never as a
    filter - the earlier version of this module tried using a person's *past* jobs as
    a family filter and it hid 1,038 of 1,833 postings for exactly the reason this
    stays separate: where you have worked and what you studied are different signals,
    and neither should silently narrow the search on its own.
    """
    subject = (profile.get("field_of_study") or "").lower()
    headline = (profile.get("headline") or "").lower()
    found, seen = [], set()
    for key, families in SUBJECT_FAMILIES.items():
        if key in subject or key in headline:
            for family in families:
                if family not in seen:
                    seen.add(family)
                    found.append(family)
    return found[:4]


def _explain(profile: dict, stage: str, titles: list[str]) -> list[str]:
    """Plain sentences saying what was read and what it changed. Shown next to the
    suggestions so nothing about the inference is hidden."""
    lines = []
    degree = profile.get("degree") or ""
    subject = profile.get("field_of_study") or ""
    where = profile.get("institution") or ""
    if degree or subject:
        lines.append(f"Read a {degree or 'degree'}{' in ' + subject if subject else ''}"
                     f"{' at ' + where if where else ''}.")
    year = profile.get("graduation_year")
    if year:
        lines.append(f"Finishing {year}, so you are searching as a {stage}.")
    else:
        lines.append(f"Searching as a {stage}, based on your qualifications and dates.")
    skills = profile.get("skills") or []
    if skills:
        lines.append(f"Picked up {len(skills)} skills from your profile - "
                     f"{', '.join(skills[:5])}"
                     f"{' and more' if len(skills) > 5 else ''}.")
    years = profile.get("years_experience") or 0
    if years:
        lines.append(f"Counted about {years} years of work, overlapping roles once.")
    if titles:
        lines.append(f"Suggested {len(titles)} job titles to search. Delete any that "
                     f"are wrong - they are only a starting point.")
    worked = [f.replace("_", " ") for f in (profile.get("families") or [])][:4]
    if worked:
        lines.append(f"You have worked in {', '.join(worked)}. That is not used as a "
                     f"filter - where you have worked is not where you want to go - but "
                     f"tick those fields yourself if you do want them.")
    lines.append("Your name, email, phone number and address were in that file and were "
                 "not read. Nothing above identifies you on its own.")
    return lines
