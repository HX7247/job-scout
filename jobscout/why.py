"""Why this posting suits you, in a sentence you could say out loud.

Shown on the swipe card, where there is room for about forty words and a decision is
being made in two seconds. The score already says *how much* it matches; this says *what*
matched, which is the part that changes a decision.

Two engines. The rule-based one is the default and needs no key: it reads the scoring
reasons the matcher already produced and writes them as a sentence. The model version
gets the posting text as well, so it can name the thing in the advert that lines up - and
it is told, in humanise.VOICE_RULES, not to flatter anybody.

What is sent to the model: the advert (public), and the course and skills from the
profile (already public on the user's own LinkedIn). Never a name, never a contact
detail, because privacy.PROFILE_ALLOWLIST means we do not have one to send.
"""
from __future__ import annotations

import logging
import os
import re

from . import humanise
from .models import Job

log = logging.getLogger("jobscout.why")

MODEL = "claude-opus-5"

# Scoring reasons are written for the table, where space is short. These turn them into
# prose. Each entry is (pattern, prefix): the prefix replaces the matched label, and
# whatever the pattern captures is kept. No backreferences in the data - an earlier
# version used them and a mangled escape put a raw control byte in this file.
_PHRASINGS = [
    (r"^keywords:\s*", "it mentions "),
    (r"^location:\s*", "it is in "),
    (r"^title matches.*", "the title is one you are searching for"),
    (r"^direct from company board.*",
     "it came straight off their own careers page, so the link will still work"),
    (r"^posted \d+d ago.*", "it went up recently"),
    (r"^licensed sponsor.*", "they hold a sponsor licence, which matters for you"),
    (r"^remote.*", "it is remote"),
    (r"^hybrid.*", "the arrangement is hybrid, which is what you asked for"),
    (r"^salary.*", "the pay clears your floor"),
    (r"^recent.*", "it went up recently"),
]


def _tidy(reason: str) -> str:
    """Rewrite one scoring reason as something you could say out loud."""
    for pattern, prefix in _PHRASINGS:
        if re.match(pattern, reason, re.I):
            return (prefix + re.sub(pattern, "", reason, count=1, flags=re.I)).strip()
    return reason


def rule_based(job: Job, profile=None) -> dict:
    """A reason built from what the matcher already worked out. No network, no key."""
    matched = [s for s in (job.matched_skills or []) if s][:3]
    reasons = [r for r in (job.score_reasons or []) if r]

    parts: list[str] = []
    if matched:
        listed = matched[0] if len(matched) == 1 else (
            ", ".join(matched[:-1]) + " and " + matched[-1])
        parts.append(f"They ask for {listed}, and you have used all of "
                     f"{'it' if len(matched) == 1 else 'them'}."
                     if len(matched) > 1 else
                     f"They ask for {listed}, which you have used.")
    title_reason = next((r for r in reasons if "title" in r.lower()), "")
    if title_reason:
        parts.append("The title is one you are searching for.")
    elif not matched:
        parts.append("Nothing in your profile lines up with this one directly - "
                     "it matched on the search terms rather than on your skills.")

    gaps = [s for s in (job.missing_skills or []) if s][:2]
    if gaps:
        parts.append(f"They also mention {' and '.join(gaps)}, which you have not shown.")

    extras = [_tidy(r) for r in reasons
              if not re.search(r"title|skill", r, re.I)][:2]
    if extras:
        parts.append("Also: " + "; ".join(extras) + ".")

    text = " ".join(parts)
    return {"why": text, "engine": "match reasons", "confidence": _confidence(job),
            "matched": matched, "gaps": gaps}


def _confidence(job: Job) -> str:
    """How much to trust the reason, said plainly rather than as a number."""
    if not (job.matched_skills or []):
        return "low - matched on your search terms, not on your skills"
    if job.score >= 65:
        return "high"
    if job.score >= 45:
        return "moderate"
    return "low"


def ai_based(job: Job, profile=None) -> dict | None:
    """One honest paragraph from Claude, or None when no key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    tool = {
        "name": "match_reason",
        "description": "Why this advert suits this candidate, and why it might not.",
        "input_schema": {
            "type": "object",
            "properties": {
                "why": {"type": "string",
                        "description": "At most 45 words. Name the specific thing in the "
                                       "advert that lines up with the specific thing the "
                                       "candidate has done."},
                "against": {"type": "string",
                            "description": "The strongest reason to swipe left, in at "
                                           "most 25 words. Say 'nothing obvious' only "
                                           "if that is true."},
                "confidence": {"type": "string", "enum": ["high", "moderate", "low"]},
            },
            "required": ["why", "against", "confidence"],
        },
    }

    skills = ", ".join((getattr(profile, "skills", None) or [])[:20]) or "not known"
    course = getattr(profile, "field_of_study", "") or "not known"
    level = getattr(profile, "degree", "") or getattr(profile, "education_level", "") or ""
    stage = getattr(profile, "career_stage", "") or ""

    prompt = f"""Decide whether this advert suits this person, and say why in one breath.

THE ADVERT
Title: {job.title}
Employer: {job.company}
Location: {job.location or "not stated"}
{job.description[:2500]}

THE PERSON
Studying or studied: {course} ({level}), currently a {stage or "job seeker"}
Skills they have evidence for: {skills}

Rules:
- Name something concrete from the advert. Not "aligns with your background".
- If the fit is weak, say so. A card that says "weak fit, but it is local and they train
  you" is more useful than a flattering one.
- Never tell them they would be a great candidate. You do not know that.
- No second person plural, no exclamation marks.

{humanise.VOICE_RULES}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL, max_tokens=600,
            tools=[tool], tool_choice={"type": "tool", "name": "match_reason"},
            messages=[{"role": "user", "content": prompt}])
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                data = block.input
                text = humanise.soften(data.get("why", ""))
                return {
                    "why": text,
                    "against": humanise.soften(data.get("against", "")),
                    "confidence": data.get("confidence", "moderate"),
                    "engine": MODEL,
                    "voice": {"score": humanise.score(text),
                              "tells": humanise.tells(text)[:4]},
                    "matched": (job.matched_skills or [])[:4],
                    "gaps": (job.missing_skills or [])[:3],
                }
    except Exception as exc:
        log.warning("Claude unavailable for the match reason: %s", exc)
    return None


def explain(job: Job, profile=None, use_model: bool = True) -> dict:
    """The reason to show on the card. Falls back rather than failing."""
    if use_model:
        result = ai_based(job, profile)
        if result:
            return result
    return rule_based(job, profile)
