"""Turn a document into a matchable profile, keeping only what matching needs.

Everything here runs locally - the document never leaves the machine. More than that,
almost none of it is kept: the parser reads the whole file, pulls out skills, the
qualification and the subject, and drops the text. There is no field on CVProfile for
your name, your email or your phone number, and no code path that could store one,
because privacy.PROFILE_ALLOWLIST is what decides what gets written.

A CV is the second-best input. The primary one is linkedin_import, which reads facts
already public on the user's own profile. This module stays because plenty of people
have a CV to hand and no LinkedIn export yet.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger("jobscout.cv")

# Skill vocabulary. Grouped so the UI can explain *why* something matched.
SKILL_VOCAB: dict[str, list[str]] = {
    "languages": [
        "python", "java", "javascript", "typescript", "c++", "c#", "c", "go", "golang",
        "rust", "ruby", "php", "swift", "kotlin", "scala", "r", "matlab", "sql", "vba",
        "bash", "shell", "perl", "julia", "fortran", "solidity", "dart",
    ],
    "data": [
        "pandas", "numpy", "scipy", "scikit-learn", "sklearn", "tensorflow", "pytorch",
        "keras", "machine learning", "deep learning", "nlp", "computer vision",
        "data analysis", "data science", "statistics", "regression", "time series",
        "power bi", "tableau", "looker", "dbt", "spark", "hadoop", "airflow", "etl",
        "data visualisation", "data visualization", "jupyter", "statsmodels",
    ],
    "web": [
        "react", "angular", "vue", "svelte", "next.js", "node.js", "express", "django",
        "flask", "fastapi", "spring", "rails", "html", "css", "tailwind", "graphql",
        "rest api", "microservices",
    ],
    "cloud_devops": [
        "aws", "azure", "gcp", "google cloud", "docker", "kubernetes", "terraform",
        "jenkins", "ci/cd", "github actions", "gitlab", "linux", "ansible", "git",
        "postgresql", "mysql", "mongodb", "redis", "kafka", "elasticsearch",
    ],
    "engineering": [
        "cad", "solidworks", "autocad", "catia", "fusion 360", "ansys", "abaqus",
        "comsol", "simulink", "labview", "plc", "scada", "finite element", "fea", "cfd",
        "thermodynamics", "fluid mechanics", "control systems", "signal processing",
        "pcb", "altium", "verilog", "vhdl", "fpga", "embedded", "arduino", "raspberry pi",
        "mechanical design", "electrical", "manufacturing", "lean", "six sigma",
        "revit", "gis", "structural analysis", "hvac", "cnc", "3d printing",
    ],
    "safety_quality": [
        "health and safety", "hse", "ehs", "iso 9001", "iso 45001", "iso 14001",
        "risk assessment", "coshh", "nebosh", "iosh", "quality assurance", "auditing",
        "compliance", "regulatory", "sustainability", "environmental",
    ],
    "finance": [
        "financial modelling", "financial modeling", "valuation", "excel", "bloomberg",
        "derivatives", "equities", "fixed income", "portfolio", "risk management",
        "quantitative", "trading", "backtesting", "algorithmic trading", "econometrics",
        "accounting", "ifrs",
    ],
    "soft": [
        "project management", "agile", "scrum", "stakeholder", "leadership", "teamwork",
        "communication", "presentation", "problem solving", "time management",
        "report writing", "customer service", "mentoring",
    ],
}

ALL_SKILLS = {s for group in SKILL_VOCAB.values() for s in group}
SKILL_GROUP = {s: g for g, items in SKILL_VOCAB.items() for s in items}

DEGREE_RE = re.compile(
    r"\b(BEng|MEng|BSc|MSc|BA|MA|MBA|PhD|DPhil|MPhil|HND|HNC|BTEC|A-?Level|Foundation|"
    r"Associate|Bachelor'?s?|Master'?s?|Doctorate|Diplom|Licence|Laurea|Ingenieur|"
    r"Baccalaur[ée]at|Abitur|B\.?Tech|M\.?Tech|BBA|LLB|LLM|MD|JD)\b", re.I)
GRAD_YEAR_RE = re.compile(r"\b(20[2-3]\d)\b")
# Contact-detail patterns deliberately live in privacy.py, which removes them, rather
# than here, where an earlier version used them to *collect* an email address.

STOPWORDS = set("""
a an and are as at be but by for from has have i in is it its of on or that the to was
were will with my me we our you your they their this these those he she his her them us
been being do does did doing not no nor so than then there here when where which who whom
while about above after again against all also am any because before below between both
each few further more most other own same some such only very can just should now able
""".split())


@dataclass
class CVProfile:
    """What the app knows about the person searching. Nothing here identifies them.

    One shape for both input routes - a LinkedIn export fills more of it than a CV can,
    and scoring only ever reads ``skills``, so a partially filled profile still works.
    """
    skills: list[str] = field(default_factory=list)
    skills_by_group: dict[str, list[str]] = field(default_factory=dict)
    custom_skills: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    degrees: list[str] = field(default_factory=list)
    degree: str = ""                 # the top one, written as a person would ("MEng")
    education_level: str = ""        # secondary|foundation|diploma|bachelors|masters|doctorate
    field_of_study: str = ""
    institution: str = ""
    graduation_year: str = ""
    years_experience: float = 0.0
    recent_titles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    region: str = ""                 # coarse: "London, England". Never a street address.
    headline: str = ""               # the user's own public one-liner
    career_stage: str = ""
    source: str = ""                 # linkedin_export|linkedin_pdf|cv|manual
    imported_at: str = ""
    word_count: int = 0

    def to_dict(self) -> dict:
        """Safe to send to the browser, to a model, or into a log."""
        from . import privacy
        data = privacy.clean_profile_dict(asdict(self))
        data["career_stage"] = self.career_stage
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CVProfile":
        """Rebuild from a stored profile, ignoring anything not on the allowlist."""
        from . import privacy
        clean = privacy.clean_profile_dict(data or {})
        stage = (data or {}).get("career_stage", "")
        known = {f.name for f in fields(cls)}
        profile = cls(**{k: v for k, v in clean.items() if k in known})
        profile.career_stage = stage
        return profile


# ------------------------------------------------------------------ extraction
def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception as exc:
        log.warning("pypdf failed on %s (%s), trying pdfminer", path.name, exc)
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception as exc:
        log.error("could not read PDF %s: %s", path.name, exc)
        return ""


def _read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        log.error("python-docx not installed")
        return ""
    try:
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    except Exception as exc:
        log.error("could not read DOCX %s: %s", path.name, exc)
        return ""


def read_document(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".docx", ".dotx"):
        return _read_docx(path)
    if suffix in (".txt", ".md"):
        return path.read_text("utf-8", errors="ignore")
    raise ValueError(f"unsupported CV format: {suffix}")


# One alternation over the whole vocabulary, built once at import time, rather than a
# separate re.search per skill on every call. The per-skill loop cost 15 seconds of a
# 1,800-job rescore (414k individual regex searches) once measured under cProfile -
# essentially the entire cost of scoring a batch of postings was this one function.
# Longest-first so "machine learning" is matched whole rather than as "learning" with
# "machine " left dangling - Python's alternation tries branches in listed order, so
# ordering here is what makes that guarantee hold, same technique as classify.py's
# _compile and rules.py's _pattern.
_SKILL_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" +
    "|".join(re.escape(s) for s in sorted(ALL_SKILLS, key=len, reverse=True)) +
    r")(?![a-z0-9])")


def extract_skills(text: str) -> list[str]:
    """Whole-token skill matches, longest first so 'machine learning' beats 'learning'."""
    lower = f" {re.sub(r'[^a-z0-9+#./ -]', ' ', text.lower())} "
    lower = re.sub(r"\s+", " ", lower)
    # dict.fromkeys rather than a set: preserves the order matches were found in, which
    # keeps the "drop shorter matches contained in a longer one" step below deterministic.
    found = list(dict.fromkeys(m.group(0) for m in _SKILL_PATTERN.finditer(lower)))
    # drop skills fully contained in a longer match already found
    return [s for s in found if not any(s != o and s in o for o in found)]


def extract_keywords(text: str, top: int = 45) -> list[str]:
    # Trailing punctuation is part of the sentence, not the word: without the strip
    # this produced "devices." and "python." as separate keywords from "devices".
    words = [w.strip(".-") for w in
             re.findall(r"[a-zA-Z][a-zA-Z+#.-]{2,}", text.lower())]
    counts = Counter(w for w in words if w not in STOPWORDS and len(w) > 3)
    return [w for w, _ in counts.most_common(top)]


# Specific abbreviations beat the generic words - "MEng" says more than "Master".
_DEGREE_RANK = ["PHD", "DPHIL", "MD", "JD", "LLM", "MBA", "MENG", "MSC", "MPHIL",
                "MTECH", "MA", "BENG", "BSC", "BTECH", "BBA", "LLB", "BA",
                "HND", "HNC", "BTEC", "DIPLOM", "LAUREA", "LICENCE", "INGENIEUR",
                "DOCTORATE", "MASTER", "MASTERS", "BACHELOR", "BACHELORS",
                "ASSOCIATE", "FOUNDATION", "ABITUR", "BACCALAUREAT", "ALEVEL", "A-LEVEL"]


# How each qualification should be written in text a human will read.
DEGREE_DISPLAY = {
    "PHD": "PhD", "DPHIL": "DPhil", "MENG": "MEng", "BENG": "BEng", "MSC": "MSc",
    "BSC": "BSc", "MPHIL": "MPhil", "MBA": "MBA", "MTECH": "M.Tech", "BTECH": "B.Tech",
    "BBA": "BBA", "LLB": "LLB", "LLM": "LLM", "MD": "MD", "JD": "JD", "MA": "MA",
    "BA": "BA", "HND": "HND", "HNC": "HNC", "BTEC": "BTEC",
}


def display_degree(code: str) -> str:
    """Render a degree code the way a person would write it."""
    return DEGREE_DISPLAY.get((code or "").upper(), (code or "").title())


def _rank_degrees(found: list[str]) -> list[str]:
    """Most specific qualification first, so callers can just take degrees[0]."""
    seen = {d.upper().replace("'", "").rstrip("S") if d.upper() in ("MASTERS", "BACHELORS")
            else d.upper() for d in found}
    ordered = [d for d in _DEGREE_RANK if d in seen]
    return ordered + sorted(seen - set(ordered))


def build_profile(path: str | Path, extra_skills: list[str] | None = None) -> CVProfile:
    text = read_document(path)
    if not text.strip():
        raise ValueError(f"no text extracted from {path}")

    skills = extract_skills(text)
    for extra in (extra_skills or []):
        if extra.lower() not in skills:
            skills.append(extra.lower())

    by_group: dict[str, list[str]] = {}
    for skill in skills:
        by_group.setdefault(SKILL_GROUP.get(skill, "other"), []).append(skill)

    years = GRAD_YEAR_RE.findall(text)
    degrees = _rank_degrees(DEGREE_RE.findall(text))

    # The text itself is not returned. Contact details, addresses and employment
    # history stay in the file they came from; only the derived signal comes back.
    from .linkedin_import import education_level, field_of_study
    return CVProfile(
        skills=skills,
        skills_by_group=by_group,
        custom_skills=[s for s in skills if s not in ALL_SKILLS],
        keywords=extract_keywords(text),
        degrees=degrees,
        degree=display_degree(degrees[0]) if degrees else "",
        education_level=education_level(text),
        field_of_study=_subject_near_degree(text),
        graduation_year=max(years) if years else "",
        source="cv",
        imported_at=datetime.now().isoformat(timespec="seconds"),
        word_count=len(text.split()),
    )


# Two shapes a CV writes a course in: "MEng Biomedical Engineering" and the reverse,
# "Biomedical Engineering undergraduate (MEng)". Comma-splitting is wrong here - a CV
# runs the course into a prose sentence - so match around the qualification instead.
# DEGREE_RE captures its own group, which would take over match.lastindex below. Use a
# non-capturing copy so the subject is always the one named group.
_DEG = "(?:" + DEGREE_RE.pattern.replace(r"\b(", r"\b(?:", 1) + ")"
_WORDS = r"[A-Z][A-Za-z&/]*(?:\s+(?:and\s+|with\s+)?[A-Z][A-Za-z&/]*){0,3}"

_SUBJECT_AFTER = re.compile(
    _DEG + r"[\s,)(]*(?:degree\s+)?(?:in|of)?\s+(?P<subject>" + _WORDS + ")")
_SUBJECT_BEFORE = re.compile(
    r"(?P<subject>" + _WORDS + r")\s+(?:undergraduate|student|graduate|degree)?\s*\(?"
    + _DEG)

_NOT_A_SUBJECT = {"university", "college", "school", "institute", "london", "hons",
                  "honours", "class", "grade", "expected", "present", "education",
                  "linkedin", "profile", "cv", "resume", "github", "email", "phone"}


def _subject_near_degree(text: str) -> str:
    """The course name, read from the words touching the qualification.

    Line by line, because a whitespace match crosses newlines and would happily
    read the page header into the subject - which is exactly how an earlier version
    produced "LinkedIn PROFILE Biomedical Engineering".
    """
    lines = [l for l in text.splitlines() if DEGREE_RE.search(l)]
    for pattern in (_SUBJECT_BEFORE, _SUBJECT_AFTER):
        for match in (m for line in lines for m in pattern.finditer(line)):
            subject = " ".join(match.group("subject").split())
            words = [w for w in subject.split() if w.lower() not in _NOT_A_SUBJECT]
            if len(" ".join(words)) > 4:
                return " ".join(words)
    return ""
