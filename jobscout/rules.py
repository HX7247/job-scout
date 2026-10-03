"""Filters the user writes themselves, when the built-in ones do not cover it.

The app ships eleven job types and twenty-three fields of work, and that will still miss
things. Somebody wants to exclude anything mentioning a night shift. Somebody wants only
postings that say "no experience required". Somebody wants their own "close to home"
checkbox covering four specific towns. None of that belongs in a shared taxonomy, and all
of it is two lines of configuration.

A rule is a label, some terms, and what to do when they match:

    require   drop the posting unless a term appears
    exclude   drop the posting when a term appears
    boost     add points to the match score, and explain why on the row

Rules can be scoped to the title, the company, the location or the description, which
matters: "graduate" in a description is noise, "graduate" in a title is the job.

Set ``facet: true`` and the rule also becomes a checkbox in the sidebar, next to the
built-in ones, with its own live count. That is the actual feature - not "configure the
YAML", but "add the filter you wanted and tick it".
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

MODES = ("require", "exclude", "boost")
FIELDS = ("any", "title", "description", "company", "location")

# SQL column per scope, for the sidebar's count and filter. "any" spans the four.
_COLUMNS = {"title": ["title"], "company": ["company"], "location": ["location"],
            "description": ["description"],
            "any": ["title", "company", "location", "description"]}


@dataclass
class Rule:
    label: str
    terms: list[str] = field(default_factory=list)
    mode: str = "boost"
    field: str = "any"
    weight: float = 8.0
    facet: bool = False
    note: str = ""

    @property
    def key(self) -> str:
        """Stable id for the checkbox and the query string."""
        return re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_") or "rule"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["key"] = self.key
        return data


def parse(raw: list | None) -> list[Rule]:
    """Build rules from config, dropping anything malformed rather than raising.

    Config is hand-edited and comes from the browser, so a bad rule must not be able to
    stop the app loading. A rule with no terms is silently ignored - it would match
    everything or nothing, and neither is what anyone meant.
    """
    rules: list[Rule] = []
    seen: dict[str, int] = {}
    for entry in (raw or []):
        if not isinstance(entry, dict):
            continue
        terms = [str(t).strip().lower() for t in (entry.get("terms") or []) if str(t).strip()]
        label = str(entry.get("label") or "").strip()
        if not terms or not label:
            continue
        mode = str(entry.get("mode") or "boost").lower()
        scope = str(entry.get("field") or "any").lower()
        try:
            weight = float(entry.get("weight", 8.0))
        except (TypeError, ValueError):
            weight = 8.0
        rule = Rule(
            label=label[:48], terms=terms[:40],
            mode=mode if mode in MODES else "boost",
            field=scope if scope in FIELDS else "any",
            weight=max(-40.0, min(40.0, weight)),
            facet=bool(entry.get("facet")),
            note=str(entry.get("note") or "")[:200])
        # Two rules with the same label share a key, so they would render as two
        # identical checkboxes that toggle each other. Last one wins, which is what
        # someone re-adding a rule to change it expects.
        if rule.key in seen:
            rules[seen[rule.key]] = rule
        else:
            seen[rule.key] = len(rules)
            rules.append(rule)
    return rules


def _haystack(job, scope: str) -> str:
    if scope == "title":
        return job.title or ""
    if scope == "company":
        return job.company or ""
    if scope == "location":
        return job.location or ""
    if scope == "description":
        return job.description or ""
    return " ".join([job.title or "", job.company or "", job.location or "",
                     job.description or ""])


def _pattern(terms: list[str]) -> "re.Pattern":
    """Word-boundary match, longest term first.

    Plain substring matching is what made "pub" match "Global Public Sector" in the
    classifier, so user rules get the same treatment: a term matches a word, not a
    fragment. Terms containing spaces still work, and a term the user writes with a
    wildcard (``night*``) keeps the loose behaviour on purpose.
    """
    parts = []
    for term in sorted(terms, key=len, reverse=True):
        if term.endswith("*"):
            parts.append(re.escape(term[:-1]) + r"\w*")
        else:
            parts.append(re.escape(term))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


_CACHE: dict[tuple, "re.Pattern"] = {}


def matches(rule: Rule, job) -> str:
    """The matched term, or "" - the term itself so the UI can say what fired."""
    key = (rule.field, tuple(rule.terms))
    pattern = _CACHE.get(key)
    if pattern is None:
        pattern = _CACHE[key] = _pattern(rule.terms)
    found = pattern.search(_haystack(job, rule.field))
    return found.group(0) if found else ""


def hard_filter(rules: list[Rule], job) -> str | None:
    """Reason to drop this posting under the user's own rules, if any."""
    for rule in rules:
        hit = matches(rule, job)
        if rule.mode == "exclude" and hit:
            return f"your rule “{rule.label}” ({hit})"
        if rule.mode == "require" and not hit:
            return f"your rule “{rule.label}” requires one of: " \
                   f"{', '.join(rule.terms[:4])}"
    return None


def score(rules: list[Rule], job) -> tuple[float, list[str]]:
    """Points and reasons from the user's boost rules."""
    total, reasons = 0.0, []
    for rule in rules:
        if rule.mode != "boost":
            continue
        hit = matches(rule, job)
        if hit:
            total += rule.weight
            reasons.append(f"{rule.label}: {hit}"
                           + (f" ({rule.weight:+.0f})" if rule.weight else ""))
    return total, reasons


def sql_clause(rule: Rule) -> tuple[str, list]:
    """A LIKE clause for one rule, for counting and filtering in the sidebar.

    LIKE rather than the regex above: SQLite has no word-boundary operator without an
    extension, and over-counting a facet by a row or two is a cosmetic problem, whereas
    over-*matching* a hard filter is not. The filters themselves run through ``matches``.
    """
    columns = _COLUMNS.get(rule.field, _COLUMNS["any"])
    parts, params = [], []
    for term in rule.terms:
        clean = term.rstrip("*")
        for column in columns:
            parts.append(f"{column} LIKE ?")
            params.append(f"%{clean}%")
    return "(" + " OR ".join(parts) + ")", params


def facets(rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if r.facet]
