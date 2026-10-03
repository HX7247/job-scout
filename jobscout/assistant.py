"""Natural-language assistant for editing search criteria and the tracker.

Two paths, same action vocabulary:
  * Claude Opus 5 via tool use, when an API key is available - handles free-form
    phrasing ("stop showing me anything that isn't in London or hybrid").
  * A rule-based parser otherwise - covers the common commands so the feature
    still works offline and with no key.

The model never touches the database directly. It only proposes actions from a
fixed vocabulary; `apply_actions` validates and executes them. Anything that
deletes rows or spends time on the network is marked `needs_confirm` and is not
executed until the caller passes confirm=True.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from . import classify
from .config import Config
from .store import STATUSES, Store

log = logging.getLogger("jobscout.assistant")

MODEL = "claude-opus-5"

# Operations the assistant is allowed to propose.
OPS = [
    "add_title", "remove_title",
    "add_keyword", "remove_keyword",
    "add_excluded", "remove_excluded",
    "exclude_company",
    "set_locations", "set_remote_ok", "set_salary_min", "set_max_age_days",
    "set_min_score", "set_weight", "set_exclude_senior",
    "set_visa_sponsorship", "set_career_stage", "set_work_mode", "set_country",
    "set_employment_types", "set_job_families",
    "sort", "search", "filter_status", "filter_source",
    "set_status", "star",
    "export_tracker", "run_scrape", "rescore", "none",
]

DESTRUCTIVE = {"run_scrape", "rescore"}

SORT_KEYS = {"score", "date", "company", "salary", "new"}

SYSTEM = """You are the assistant inside Job Scout, a job-search tool for a UK \
university student looking for a placement year.

You turn the user's request into a list of actions from a fixed vocabulary. You do not \
execute anything yourself - the app validates and applies what you propose.

Rules:
- Only propose actions that the request actually calls for. An empty action list plus a \
helpful reply is correct when the user is just asking a question.
- Criteria changes (titles, keywords, locations, score thresholds) take effect after a \
rescore, which the app runs automatically. Do not also propose `rescore`.
- Propose `run_scrape` only when the user explicitly asks to fetch new jobs.
- `search` sets the free-text filter on the current view; `add_keyword` changes the \
saved criteria. Prefer `search` for "show me..." and `add_keyword` for "I care about...".
- Keep `reply` to one or two plain sentences saying what you changed. No preamble.

Field notes:
- `values`: list of strings (titles, keywords, locations, company names, job ids).
- `number`: for numeric settings (salary floor, days, score, weight value).
- `text`: for a single string (a sort key, a status name, a weight name, "true"/"false").
- Weight names: title, keywords, cv_skills, location, recency, salary, sponsorship.
- Statuses: new, shortlisted, applied, interviewing, offer, rejected, dismissed.
- `set_visa_sponsorship` text: "any", "prefer" or "require". Use "require" only when
  the user says they need sponsorship or a visa; "prefer" when they say it matters.
- `set_career_stage` text: "student", "graduate", "professional", "senior" or "any".
- `set_work_mode` values: any of "remote", "hybrid", "onsite". Pass every mode the
  user would accept, and an empty list to clear the preference.
- `set_country` text: an ISO country code such as "GB", "US", "DE".
- `set_employment_types` values: job types from this exact list (use the key, not the
  label): placement, internship, apprenticeship, graduate_scheme, weekend, zero_hours,
  part_time, seasonal, contract, volunteer, full_time. Empty list means no preference -
  every job type shows.
- `set_job_families` values: fields of work from this exact list: hospitality, retail,
  care, healthcare, education, driving, warehouse, security, cleaning,
  customer_service, software, data, engineering, science, construction, finance,
  legal, marketing, sales, hr, admin, creative, operations. Empty list means every
  field shows.

When the user describes an ideal job in a sentence, translate the whole sentence:
the role words become `add_title`, the domain words `add_keyword`, the place
`set_locations`, the arrangement `set_work_mode`, and any mention of needing a visa
`set_visa_sponsorship`.
"""

TOOL = {
    "name": "apply",
    "description": "Apply a list of Job Scout actions and reply to the user.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "One or two sentences for the user.",
            },
            "actions": {
                "type": "array",
                "description": "Actions to apply, in order. May be empty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": OPS},
                        "values": {"type": "array", "items": {"type": "string"}},
                        "number": {"type": ["number", "null"]},
                        "text": {"type": ["string", "null"]},
                    },
                    "required": ["op", "values", "number", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reply", "actions"],
        "additionalProperties": False,
    },
}


@dataclass
class Action:
    op: str
    values: list[str] = field(default_factory=list)
    number: float | None = None
    text: str | None = None

    def describe(self) -> str:
        if self.values:
            return f"{self.op}: {', '.join(self.values[:6])}"
        if self.number is not None:
            return f"{self.op}: {self.number:g}"
        if self.text:
            return f"{self.op}: {self.text}"
        return self.op


@dataclass
class AssistantResult:
    reply: str
    actions: list[Action] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    view: dict = field(default_factory=dict)      # transient view changes for the UI
    needs_confirm: list[str] = field(default_factory=list)
    config_changed: bool = False
    engine: str = "rules"

    def to_dict(self) -> dict:
        return {
            "reply": self.reply,
            "actions": [a.describe() for a in self.actions],
            "applied": self.applied,
            "view": self.view,
            "needs_confirm": self.needs_confirm,
            "config_changed": self.config_changed,
            "engine": self.engine,
        }


# --------------------------------------------------------------------- rules
_NUM = r"(\d[\d,]*)"


def parse_rules(message: str) -> tuple[str, list[Action]]:
    """Keyword parser used when no API key is configured."""
    text = message.strip()
    low = text.lower()
    actions: list[Action] = []
    notes: list[str] = []

    def quoted(after: str) -> list[str]:
        """Pull the argument list following a trigger word."""
        match = re.search(rf"{after}\s+(.+)$", low)
        if not match:
            return []
        tail = re.split(r"\band\b|,|;", match.group(1))
        return [t.strip(" '\".") for t in tail if t.strip(" '\".")]

    if re.search(r"\b(scrape|refresh|fetch|find new|search again|update jobs)\b", low):
        actions.append(Action("run_scrape"))
        notes.append("run a fresh scrape")

    if re.search(r"\bexport|spreadsheet|tracker|excel\b", low):
        mode = "append" if "append" in low or "my sheet" in low else "new"
        actions.append(Action("export_tracker", text=mode))
        notes.append(f"export the tracker ({mode})")

    # Allow filler between the keyword and the number: "score floor to 50",
    # "minimum match of at least 45", "score >= 60".
    match = re.search(rf"(?:score|match)\w*\b[^\d\n]{{0,24}}?{_NUM}", low)
    if match:
        actions.append(Action("set_min_score", number=float(match.group(1).replace(",", ""))))
        notes.append(f"minimum score {match.group(1)}")

    # "I don't want anything unpaid or commission only" -> exclusion keywords
    match = re.search(r"(?:don'?t|do not|no longer)\s+want\s+(?:any(?:thing)?\s+)?(.+)$", low)
    if not match:
        match = re.search(r"\b(?:without|avoid|never show)\s+(.+)$", low)
    if match:
        terms = [t.strip(" '\".") for t in re.split(r"\bor\b|\band\b|,|;", match.group(1))]
        terms = [t for t in terms if 2 < len(t) < 40]
        if terms:
            actions.append(Action("add_excluded", values=terms))
            notes.append(f"excluding {', '.join(terms)}")

    # "I need visa sponsorship" / "companies that sponsor"
    if re.search(r"\b(visa|sponsor\w*|tier 2|skilled worker)\b", low):
        need = bool(re.search(r"\b(need|require|must|only|international student)\b", low))
        actions.append(Action("set_visa_sponsorship",
                              text="require" if need else "prefer"))
        notes.append("visa sponsorship " + ("required" if need else "preferred"))

    modes = [m for m in ("remote", "hybrid", "onsite")
             if re.search(rf"\b{m}\b", low)]
    if re.search(r"\b(on[- ]site|in the office|office[- ]based)\b", low) and "onsite" not in modes:
        modes.append("onsite")
    if modes and not re.search(r"\b(no|not|exclude|hide)\s+remote\b", low):
        actions.append(Action("set_work_mode", values=modes))
        notes.append("work mode " + ", ".join(modes))

    for stage, pattern in [("student", r"\b(placement|year in industry|i am a student)\b"),
                           ("graduate", r"\b(graduate scheme|new grad|just graduated)\b"),
                           ("senior", r"\b(senior role|lead role|principal)\b"),
                           ("professional", r"\b(experienced|mid[- ]level|few years)\b")]:
        if re.search(pattern, low):
            actions.append(Action("set_career_stage", text=stage))
            notes.append(f"career stage {stage}")
            break

    if re.search(r"\b(hide|no|exclude|not?)\s+senior\b", low):
        actions.append(Action("set_exclude_senior", text="true"))
        notes.append("hiding senior roles")

    match = re.search(rf"(?:salary|pay|paying)\D{{0,12}}{_NUM}", low)
    if match:
        value = float(match.group(1).replace(",", ""))
        if value < 1000:
            value *= 1000
        actions.append(Action("set_salary_min", number=value))
        notes.append(f"salary floor {value:,.0f}")

    match = re.search(rf"(?:last|past|within)\s+{_NUM}\s*(day|week|month)", low)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        actions.append(Action("set_max_age_days", number=days))
        notes.append(f"posted in the last {days} days")

    match = re.search(r"\bsort (?:by |on )?(\w+)", low)
    if match and match.group(1) in SORT_KEYS:
        actions.append(Action("sort", text=match.group(1)))
        notes.append(f"sort by {match.group(1)}")

    for trigger, op in [(r"add (?:the )?title", "add_title"),
                        (r"remove (?:the )?title", "remove_title"),
                        (r"(?:add|include) (?:the )?keywords?", "add_keyword"),
                        (r"(?:remove|drop) (?:the )?keywords?", "remove_keyword"),
                        (r"(?:exclude|block|hide) (?:the )?(?:keywords?|words?)", "add_excluded")]:
        values = quoted(trigger)
        if values:
            actions.append(Action(op, values=values))
            notes.append(f"{op.replace('_', ' ')} {', '.join(values)}")

    values = quoted(r"(?:exclude|hide|block|no more) (?:jobs from |roles from )?(?:company|companies|employer)")
    if values:
        actions.append(Action("exclude_company", values=values))
        notes.append(f"exclude {', '.join(values)}")

    # Places the country registry knows - but only when the sentence is actually
    # talking about a location. Several city names are ordinary English words
    # ("Reading", "Cork"), so require either a locational preposition in front or a
    # capital letter in what the user typed.
    from . import geo
    places = []
    for match in geo._LOOKUP_RE.finditer(low):
        term = match.group(1)
        if term.lower() not in geo._TERM_TO_CODE:
            continue
        before = low[max(0, match.start() - 14):match.start()]
        prepositioned = re.search(r"\b(in|near|around|based in|located in|from|at)\s+$",
                                  before) is not None
        as_typed = text[match.start():match.end()]
        if prepositioned or as_typed[:1].isupper():
            places.append(term.title())
    if places:
        seen, unique = set(), []
        for place in places:
            if place.lower() not in seen:
                seen.add(place.lower())
                unique.append(place)
        actions.append(Action("set_locations", values=unique[:4]))
        notes.append("location " + ", ".join(unique[:4]))

    if re.search(r"\b(no|hide|exclude) (remote|wfh)\b", low):
        actions.append(Action("set_remote_ok", text="false"))
        notes.append("hide remote roles")
    elif re.search(r"\b(allow|include|show) remote\b", low):
        actions.append(Action("set_remote_ok", text="true"))
        notes.append("allow remote roles")

    # Job type and field of work - "only part-time and internship roles", "I want
    # retail or hospitality work". Matched on the canonical labels/keys rather than
    # the looser classify.py alias table (which includes words like "office" and
    # "support" that are too generic to fire a filter on their own), so this stays
    # precise even though it is not exhaustive - anything it misses is still one click
    # away on the Criteria page's checkboxes.
    def _clauses(key: str, label: str) -> list[tuple[str, str]]:
        # A label like "Placement / year in industry" or "Zero hours / flexible" packs
        # two ways of saying the same thing - keep both as independent match candidates
        # rather than only the first, or "year in industry" (arguably the more common
        # phrase among UK students than "placement" on its own) is never recognised.
        parts = re.split(r" / | & ", label.lower())
        return [(key, re.sub(r"\s*\(.*\)$", "", p).strip()) for p in parts if p.strip()]

    _vocab = classify.choices()
    _kind_terms = [pair for k in _vocab["employment"] for pair in _clauses(k["key"], k["label"])]
    _family_terms = [pair for f in _vocab["families"] for pair in _clauses(f["key"], f["label"])]

    def _term_pattern(label: str) -> str:
        # "part-time" has to catch "part time" too - a hyphen in the label is the far
        # less common way people actually type it in a chat message.
        core = re.escape(label).replace(r"\-", r"[\s-]+").replace(r"\ ", r"[\s-]+")
        return rf"(?<![a-z]){core}(?![a-z])"

    def _matched_keys(terms: list[tuple[str, str]]) -> list[str]:
        found, seen = [], set()
        for key, label in sorted(terms, key=lambda t: -len(t[1])):
            if key not in seen and re.search(_term_pattern(label), low):
                seen.add(key)
                found.append(key)
        return found

    kinds = _matched_keys(_kind_terms)
    if kinds:
        actions.append(Action("set_employment_types", values=kinds))
        notes.append("job type: " + ", ".join(kinds))

    families = _matched_keys(_family_terms)
    if families:
        actions.append(Action("set_job_families", values=families))
        notes.append("field of work: " + ", ".join(families))

    if re.search(r"\b(show|find|filter)\b", low) and not actions:
        term = re.sub(r"^\W*(show|find|filter)( me)?( all| any)?\s*", "", low).strip(" .?")
        term = re.sub(r"\b(jobs?|roles?|positions?)\b", "", term).strip()
        if term:
            actions.append(Action("search", text=term))
            notes.append(f"filter to '{term}'")

    if not actions:
        return ("I did not recognise that as a change I can make. Try things like "
                "\"only London\", \"minimum score 50\", \"exclude keyword unpaid\", "
                "\"sort by date\", or \"export the tracker\".", [])

    return "Applied: " + "; ".join(notes) + ".", actions


# ----------------------------------------------------------------------- LLM
def parse_llm(message: str, context: dict) -> tuple[str, list[Action]] | None:
    """Ask Claude to turn the message into actions. Returns None if unavailable."""
    try:
        import anthropic
    except ImportError:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            tools=[TOOL],
            messages=[{
                "role": "user",
                "content": (
                    f"Current search criteria:\n{context.get('criteria')}\n\n"
                    f"Current view: {context.get('view')}\n"
                    f"Jobs in database: {context.get('total')}\n\n"
                    f"Request: {message}"
                ),
            }],
        )
    except Exception as exc:
        log.warning("assistant LLM call failed: %s", exc)
        return None

    if response.stop_reason == "refusal":
        return ("I can't help with that request.", [])

    for block in response.content:
        if block.type == "tool_use" and block.name == "apply":
            data = block.input
            actions = []
            for raw in data.get("actions", []):
                if raw.get("op") in OPS:
                    actions.append(Action(
                        op=raw["op"],
                        values=[str(v) for v in (raw.get("values") or [])],
                        number=raw.get("number"),
                        text=raw.get("text"),
                    ))
            return data.get("reply", ""), actions

    text = " ".join(b.text for b in response.content if b.type == "text")
    return (text.strip() or "I could not work out an action for that.", [])


# ------------------------------------------------------------------- execute
def apply_actions(actions: list[Action], cfg: Config, store: Store,
                  confirm: bool = False) -> AssistantResult:
    """Validate and execute actions. Config edits are saved; risky ops are gated."""
    result = AssistantResult(reply="", actions=actions)
    search = cfg.search

    def add_unique(target: list[str], values: list[str]) -> list[str]:
        added = []
        lower = {v.lower() for v in target}
        for value in values:
            if value.lower() not in lower:
                target.append(value)
                lower.add(value.lower())
                added.append(value)
        return added

    def drop(target: list[str], values: list[str]) -> list[str]:
        wanted = {v.lower() for v in values}
        removed = [t for t in target if t.lower() in wanted]
        target[:] = [t for t in target if t.lower() not in wanted]
        return removed

    for action in actions:
        op = action.op

        if op in DESTRUCTIVE and not confirm:
            result.needs_confirm.append(op)
            continue

        if op == "none":
            continue

        elif op == "add_title":
            added = add_unique(search.titles, action.values)
            if added:
                result.applied.append(f"added titles: {', '.join(added)}")
                result.config_changed = True
        elif op == "remove_title":
            removed = drop(search.titles, action.values)
            if removed:
                result.applied.append(f"removed titles: {', '.join(removed)}")
                result.config_changed = True

        elif op == "add_keyword":
            added = add_unique(search.keywords_any, action.values)
            if added:
                result.applied.append(f"added keywords: {', '.join(added)}")
                result.config_changed = True
        elif op == "remove_keyword":
            removed = drop(search.keywords_any, action.values)
            if removed:
                result.applied.append(f"removed keywords: {', '.join(removed)}")
                result.config_changed = True

        elif op == "add_excluded":
            added = add_unique(search.keywords_excluded, action.values)
            if added:
                result.applied.append(f"now excluding: {', '.join(added)}")
                result.config_changed = True
        elif op == "remove_excluded":
            removed = drop(search.keywords_excluded, action.values)
            if removed:
                result.applied.append(f"no longer excluding: {', '.join(removed)}")
                result.config_changed = True

        elif op == "exclude_company":
            added = add_unique(search.exclude_companies, action.values)
            if added:
                result.applied.append(f"excluding companies: {', '.join(added)}")
                result.config_changed = True

        elif op == "set_locations" and action.values:
            search.locations = list(action.values)
            result.applied.append(f"locations set to {', '.join(action.values)}")
            result.config_changed = True

        elif op == "set_remote_ok" and action.text is not None:
            search.remote_ok = str(action.text).lower() in ("true", "yes", "1", "on")
            result.applied.append(f"remote roles {'allowed' if search.remote_ok else 'hidden'}")
            result.config_changed = True

        elif op == "set_visa_sponsorship" and action.text:
            mode = action.text.strip().lower()
            if mode in ("any", "prefer", "require"):
                search.visa_sponsorship = mode
                result.applied.append(f"visa sponsorship: {mode}")
                result.config_changed = True

        elif op == "set_career_stage" and action.text:
            stage = action.text.strip().lower()
            if stage in ("student", "graduate", "professional", "senior", "any"):
                search.career_stage = stage
                result.applied.append(f"career stage: {stage}")
                result.config_changed = True

        elif op == "set_employment_types":
            # normalise_kind turns loose phrasing ("grad", "temp", "full time") into the
            # canonical slug the filter actually stores - same normaliser api_config
            # uses for the checkboxes, so an assistant change and a manual tick behave
            # identically. An unrecognised word is dropped rather than stored as junk
            # that would silently match nothing.
            valid_kinds = {k["key"] for k in classify.choices()["employment"]}
            kinds = [classify.normalise_kind(v) for v in action.values if v.strip()]
            kinds = [k for k in kinds if k in valid_kinds]
            search.employment_types = kinds
            result.applied.append(
                f"job type: {', '.join(kinds)}" if kinds else "job type: no preference")
            result.config_changed = True

        elif op == "set_job_families":
            valid_families = {f["key"] for f in classify.choices()["families"]}
            families = [classify.normalise_family(v) for v in action.values if v.strip()]
            families = [f for f in families if f in valid_families]
            search.job_families = families
            result.applied.append(
                f"field of work: {', '.join(families)}" if families
                else "field of work: no preference")
            result.config_changed = True

        elif op == "set_work_mode":
            modes = [v.strip().lower() for v in action.values
                     if v.strip().lower() in ("remote", "hybrid", "onsite")]
            search.work_modes = modes
            result.applied.append(
                f"work mode: {', '.join(modes)}" if modes else "work mode: no preference")
            result.config_changed = True

        elif op == "set_country" and action.text:
            from . import geo
            found = geo.get(action.text)
            if found:
                search.country = found.code
                result.applied.append(f"market: {found.name}")
                result.config_changed = True

        elif op == "set_exclude_senior" and action.text is not None:
            search.exclude_senior = str(action.text).lower() in ("true", "yes", "1", "on")
            state = "hidden" if search.exclude_senior else "shown"
            result.applied.append(f"senior roles {state}")
            result.config_changed = True

        elif op == "set_salary_min" and action.number is not None:
            search.salary_min = int(action.number) or None
            result.applied.append(f"salary floor {search.salary_min or 'cleared'}")
            result.config_changed = True

        elif op == "set_max_age_days" and action.number is not None:
            search.max_age_days = int(action.number) or None
            result.applied.append(f"max age {search.max_age_days} days")
            result.config_changed = True

        elif op == "set_min_score" and action.number is not None:
            cfg.min_score = max(0.0, min(100.0, float(action.number)))
            result.applied.append(f"minimum score {cfg.min_score:g}")
            result.config_changed = True

        elif op == "set_weight" and action.text and action.number is not None:
            name = action.text.strip().lower()
            if hasattr(cfg.weights, name):
                setattr(cfg.weights, name, float(action.number))
                result.applied.append(f"weight {name} = {action.number:g}")
                result.config_changed = True

        elif op == "sort" and action.text in SORT_KEYS:
            result.view["order"] = action.text
            result.applied.append(f"sorted by {action.text}")

        elif op == "search" and action.text:
            result.view["search"] = action.text
            result.applied.append(f"filtered to '{action.text}'")

        elif op == "filter_status" and action.text:
            if action.text in STATUSES or action.text == "all":
                result.view["status"] = action.text
                result.applied.append(f"showing status '{action.text}'")

        elif op == "filter_source" and action.text:
            result.view["source"] = action.text
            result.applied.append(f"showing source '{action.text}'")

        elif op == "set_status" and action.text in STATUSES:
            changed = sum(1 for jid in action.values if store.set_status(jid, action.text))
            if changed:
                result.applied.append(f"{changed} job(s) marked {action.text}")

        elif op == "star":
            for jid in action.values:
                store.toggle_star(jid)
            if action.values:
                result.applied.append(f"starred {len(action.values)} job(s)")

        elif op == "export_tracker":
            result.view["export"] = action.text or "new"
            result.applied.append("tracker export queued")

        elif op in DESTRUCTIVE:
            result.view[op] = True
            result.applied.append(f"{op.replace('_', ' ')} started")

    if result.config_changed:
        cfg.save()
    return result


def answer_question(message: str, cfg: Config, store: Store) -> str | None:
    """Answer the handful of factual questions the rules engine can settle locally."""
    low = message.lower()
    if "?" not in message and not re.match(r"^\s*(how|what|which|who|when|where)\b", low):
        return None
    stats = store.stats()

    if re.search(r"\bhow many\b", low):
        if "compan" in low:
            return (f"{len(stats['top_companies'])} companies appear in the top slice; "
                    f"{stats['total']} postings are stored in total.")
        if "applied" in low or "status" in low:
            return "Status breakdown: " + ", ".join(
                f"{k} {v}" for k, v in stats["by_status"].items())
        return (f"{stats['total']} postings are stored, {stats['strong_matches']} of them "
                f"scoring 60 or above. Average score is {stats['avg_score']}.")

    if re.search(r"\b(criteria|searching for|looking for|settings)\b", low):
        return (f"Titles: {', '.join(cfg.search.titles[:6])}... | "
                f"Locations: {', '.join(cfg.search.locations)} | "
                f"Min score: {cfg.min_score:g} | "
                f"Max age: {cfg.search.max_age_days} days | "
                f"Remote allowed: {cfg.search.remote_ok}")

    if re.search(r"\b(sources?|where|boards?)\b", low):
        return "Sources returning jobs: " + ", ".join(
            f"{k} ({v})" for k, v in list(stats["by_source"].items())[:10])

    if re.search(r"\b(best|top|strongest)\b", low):
        top = store.query(limit=5)
        if not top:
            return "Nothing stored yet - run a scrape first."
        return "Top matches: " + "; ".join(
            f"{j['title']} at {j['company']} ({j['score']:.0f})" for j in top)

    return None


def handle(message: str, cfg: Config, store: Store, view: dict | None = None,
           confirm: bool = False) -> AssistantResult:
    """Entry point: parse the message, apply what it asks for, report back."""
    context = {
        "criteria": {
            "titles": cfg.search.titles,
            "keywords_any": cfg.search.keywords_any[:20],
            "keywords_excluded": cfg.search.keywords_excluded,
            "locations": cfg.search.locations,
            "remote_ok": cfg.search.remote_ok,
            "salary_min": cfg.search.salary_min,
            "max_age_days": cfg.search.max_age_days,
            "min_score": cfg.min_score,
            "exclude_senior": cfg.search.exclude_senior,
        },
        "view": view or {},
        "total": store.stats()["total"],
    }

    engine = "claude"
    parsed = parse_llm(message, context)
    if parsed is None:
        engine = "rules"
        parsed = parse_rules(message)
        if not parsed[1]:
            answer = answer_question(message, cfg, store)
            if answer:
                parsed = (answer, [])

    reply, actions = parsed
    result = apply_actions(actions, cfg, store, confirm=confirm)
    result.reply = reply
    result.actions = actions
    result.engine = engine
    return result
