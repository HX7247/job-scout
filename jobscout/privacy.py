"""Data minimisation: what this app is allowed to keep about the person using it.

The rule, and it is enforced here rather than merely documented: Job Scout keeps only
facts that are **already public on the user's own LinkedIn profile**, and only in the
coarsest form that still makes matching work. Everything else is read, used once in
memory, and dropped before anything is written to disk.

Concretely:

  KEPT (public, non-identifying on its own)
    education level, degree abbreviation, field of study, institution
    skills, languages, industries worked in, coarse region ("London, England")
    years of experience, recent job titles

  DROPPED, even when handed to us
    name, maiden name, email, phone, postal address, date of birth, national ID
    profile photo, LinkedIn member ID, connection list, messages, IP, salary history
    the raw text of a CV, and the path to it

Two functions do the work. ``scrub`` removes identifiers from free text we are about to
store or send to a model. ``clean_profile_dict`` drops any key not on the allowlist, so a
new field cannot leak by being added upstream and forgotten here.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------- what may be kept
# Allowlist, not blocklist. A field added to CandidateProfile does not become storable
# until its name appears here, which is the point.
PROFILE_ALLOWLIST = {
    "skills", "skills_by_group", "custom_skills", "keywords",
    "education_level", "degree", "degrees", "field_of_study", "institution",
    "graduation_year", "years_experience", "recent_titles", "industries", "families",
    "languages", "region", "headline", "source", "imported_at", "word_count",
}

# Fields we actively expect to be handed and actively refuse. Listed so the audit can
# assert they are absent, and so a reader can see the intent without reading the code.
NEVER_STORED = {
    "first_name", "last_name", "maiden_name", "full_name", "name", "email",
    "email_address", "phone", "phone_number", "mobile", "address", "postal_code",
    "zip_code", "birth_date", "date_of_birth", "photo", "picture", "profile_photo",
    "member_id", "linkedin_id", "connections", "messages", "raw_text", "source_file",
    "cv_path", "ip", "national_insurance", "ssn", "passport", "salary_history",
    "twitter_handles", "instant_messengers", "websites",
}

# ------------------------------------------------------------------- text scrubbing
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
# International: optional +CC then a run of digits with the usual separators. The digit
# count is checked in _phone() rather than in the pattern, because a loose version of
# this matched "2026-09-21" and replaced an ISO timestamp with "[phone removed]".
PHONE = re.compile(r"(?:\+\d{1,3}[\s.\-()]*)?(?:\(?\d{2,5}\)?[\s.\-]*){2,5}\d{2,4}")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
# Real numbers have at least 9 digits once separators are ignored; dates and years do not.
MIN_PHONE_DIGITS = 9
URL_PROFILE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w\-%]+", re.I)
POSTCODE_GB = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
POSTCODE_US = re.compile(r"\b\d{5}(?:-\d{4})?\b")
# Windows and POSIX home directories give away the account name.
HOME_PATH = re.compile(r"(?:[A-Za-z]:\\Users\\[^\\/:*?\"<>|\r\n]+|/(?:home|Users)/[^/\s]+)")
NI_NUMBER = re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b")
DOB = re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[/.\-](?:0?[1-9]|1[0-2])[/.\-](?:19|20)\d{2}\b")

_RULES = [
    (EMAIL, "[email removed]"),
    (URL_PROFILE, "[profile link removed]"),
    (NI_NUMBER, "[id removed]"),
    (HOME_PATH, "[path removed]"),
    (POSTCODE_GB, "[postcode removed]"),
    (DOB, "[date removed]"),
]


def _phone(text: str) -> str:
    """Replace phone numbers only. A date, a year or a salary is left alone."""
    def swap(match: "re.Match") -> str:
        span = match.group(0)
        if ISO_DATE.fullmatch(span.strip()):
            return span
        digits = sum(c.isdigit() for c in span)
        return "[phone removed]" if digits >= MIN_PHONE_DIGITS else span
    return PHONE.sub(swap, text)


def scrub(text: str | None, aggressive: bool = False) -> str:
    """Remove personal identifiers from free text.

    ``aggressive`` adds US ZIP codes, which are five bare digits and therefore also
    match salaries, years and reference numbers. Only use it on text that is about to
    leave the machine.
    """
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    out = _phone(out)
    if aggressive:
        out = POSTCODE_US.sub("[postcode removed]", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def contains_identifier(text: str | None) -> list[str]:
    """Which kinds of identifier a string still holds. Used by the audit and tests."""
    if not text:
        return []
    found = []
    for name, pattern in (("email", EMAIL), ("profile link", URL_PROFILE),
                          ("national id", NI_NUMBER), ("home path", HOME_PATH),
                          ("postcode", POSTCODE_GB), ("date of birth", DOB)):
        if pattern.search(str(text)):
            found.append(name)
    return found


def clean_profile_dict(data: dict) -> dict:
    """Keep only allowlisted keys, and scrub what survives.

    Silently dropping is right here: the caller is usually a parser handed a file full
    of personal data, and the correct behaviour is to take the two useful columns and
    forget the rest, not to error.
    """
    clean: dict = {}
    for key, value in (data or {}).items():
        if key not in PROFILE_ALLOWLIST:
            continue
        if isinstance(value, str):
            clean[key] = scrub(value)
        elif isinstance(value, list):
            clean[key] = [scrub(v) if isinstance(v, str) else v for v in value]
        elif isinstance(value, dict):
            clean[key] = {k: [scrub(v) if isinstance(v, str) else v for v in (vals or [])]
                          for k, vals in value.items()}
        else:
            clean[key] = value
    return clean


# --------------------------------------------------------------------- inventory
def inventory(root) -> list[dict]:
    """Everything this install holds, so the Privacy panel can show it plainly.

    One row per store, with what it contains and whether it is personal. Written as
    data rather than prose so the UI can render it and the purge button can act on it.
    """
    from pathlib import Path
    root = Path(root)

    def size(*parts) -> int:
        path = root.joinpath(*parts)
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return path.stat().st_size if path.exists() else 0

    return [
        {"what": "Job postings", "where": "data/jobscout.db",
         "personal": False, "bytes": size("data", "jobscout.db"),
         "detail": "Employers' public adverts, plus your status and notes per job. "
                   "Nothing here identifies you."},
        {"what": "Your derived profile", "where": "data/accounts/<you>/profile.json",
         "personal": True, "bytes": size("data", "accounts"),
         "detail": "Education level, degree, field of study, institution, skills, "
                   "languages, coarse region, years of experience, recent job titles. "
                   "All of it already public on your LinkedIn. No name, email, phone "
                   "or address is kept."},
        {"what": "Your search criteria", "where": "config.yaml / account config",
         "personal": False, "bytes": size("config.yaml"),
         "detail": "Titles, keywords, locations and filters you set."},
        {"what": "Login", "where": "data/accounts.db",
         "personal": True, "bytes": size("data", "accounts.db"),
         "detail": "A username you chose and a scrypt hash of your password. "
                   "No email address, so a lost password cannot be reset by mail."},
        {"what": "Page cache", "where": "data/cache/",
         "personal": False, "bytes": size("data", "cache"),
         "detail": "Copies of employers' public career pages, to avoid re-fetching."},
        {"what": "Exported workbooks", "where": "out/",
         "personal": False, "bytes": size("out"),
         "detail": "Job data and your own notes. Written by you, for you."},
    ]
