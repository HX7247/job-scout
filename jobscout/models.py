"""Canonical job record shared by every source adapter."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_ENTITY = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&ndash;": "-", "&mdash;": "-",
    "&rsquo;": "'", "&lsquo;": "'", "&ldquo;": '"', "&rdquo;": '"', "&pound;": "£",
}


def _unescape(text: str) -> str:
    for entity, char in _ENTITY.items():
        text = text.replace(entity, char)
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)


def strip_html(value: str | None) -> str:
    """Flatten an HTML fragment to readable plain text.

    Entities are decoded *before* tags are stripped, and the whole pass repeats while
    it keeps finding markup. Several ATS APIs return HTML that is itself escaped
    (`&lt;p&gt;`), so decoding last would leave literal tags in the output.
    """
    if not value:
        return ""
    text = _unescape(str(value))
    for _ in range(3):                        # escaped-inside-escaped does happen
        if "<" not in text and "&" not in text:
            break
        text = text.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
        text = text.replace("<br />", "\n").replace("</li>", "\n").replace("</div>", "\n")
        text = _TAG.sub(" ", text)
        decoded = _unescape(text)
        if decoded == text:
            break
        text = decoded
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def clean(value: str | None) -> str:
    return _WS.sub(" ", (value or "").replace(" ", " ")).strip()


def parse_date(value: Any) -> str | None:
    """Best-effort ISO-8601 date string from whatever a source hands us."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            dt = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


@dataclass
class Contact:
    """A named human attached to a posting, as published by the source itself."""
    name: str
    title: str = ""
    source: str = ""
    profile_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    source: str                    # adapter name, e.g. "greenhouse"
    source_kind: str               # ats_direct | aggregator | board
    company: str
    title: str
    url: str
    external_id: str = ""
    location: str = ""
    remote: bool = False
    description: str = ""
    salary_raw: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    employment_type: str = ""
    posted_at: str | None = None
    closes_at: str | None = None
    department: str = ""
    # Filled in by __post_init__ from the title/description; "" means the posting
    # never said, which is different from saying "full time".
    employment_kind: str = ""
    job_family: str = ""
    contacts: list[Contact] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # populated downstream
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    _TEXT_FIELDS = ("source", "source_kind", "company", "title", "url", "external_id",
                    "location", "description", "salary_raw", "salary_currency",
                    "employment_type", "department", "employment_kind", "job_family")

    def __post_init__(self) -> None:
        # Sources are inconsistent about null vs missing vs "" - normalise once, here,
        # so nothing downstream has to defend against None.
        for field_name in self._TEXT_FIELDS:
            value = getattr(self, field_name)
            setattr(self, field_name, "" if value is None else str(value))
        self.company = clean(self.company)
        self.title = clean(self.title)
        self.location = clean(self.location)
        self.url = self.url.strip()
        self.remote = bool(self.remote)

        # What kind of work this is, and on what basis. Sources are wildly
        # inconsistent here, so derive it rather than trusting the raw field.
        if not self.employment_kind or not self.job_family:
            from .classify import classify
            kind, family = classify(self.title, self.description,
                                    self.employment_type, self.department)
            self.employment_kind = self.employment_kind or kind
            self.job_family = self.job_family or family
        for num in ("salary_min", "salary_max"):
            value = getattr(self, num)
            if value is not None:
                try:
                    setattr(self, num, float(value))
                except (TypeError, ValueError):
                    setattr(self, num, None)

    @property
    def id(self) -> str:
        """Stable identity for one posting from one source."""
        key = f"{self.source}|{self.external_id or self.url}".lower()
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def dedupe_key(self) -> str:
        """Identity ACROSS sources - the same role listed twice collapses to one row."""
        company = re.sub(r"\b(ltd|limited|plc|inc|llc|group|uk|gmbh)\b", "", self.company.lower())
        company = re.sub(r"[^a-z0-9]", "", company)
        title = re.sub(r"[^a-z0-9 ]", "", self.title.lower())
        title = _WS.sub(" ", re.sub(r"\b(20\d\d|f/m/d|m/f/d|uk|london|remote|hybrid)\b", "", title)).strip()
        town = re.sub(r"[^a-z]", "", self.location.lower().split(",")[0])[:12]
        return hashlib.sha1(f"{company}|{title}|{town}".encode("utf-8")).hexdigest()[:16]

    @property
    def salary_display(self) -> str:
        if self.salary_min or self.salary_max:
            from .geo import format_salary
            return format_salary(self.salary_min, self.salary_max, self.salary_currency)
        return clean(self.salary_raw)

    def searchable(self) -> str:
        return " ".join([self.title, self.company, self.location,
                         self.department, self.description]).lower()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["id"] = self.id
        data["dedupe_key"] = self.dedupe_key
        data["salary_display"] = self.salary_display
        data.pop("raw", None)
        return data
