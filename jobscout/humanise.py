"""Catch writing that reads as machine-written, in drafts a human will send or say.

Why this exists as code rather than as a line in a prompt: models comply with "sound
natural" for a paragraph and then drift back, and the person using this app cannot tell
which paragraph drifted. So the prompt asks for a voice, and this module checks whether
it got one, and says what to change. The check runs on cover letters and interview notes
alike.

Two things it does NOT do. It does not rewrite your answers for you - an interview answer
recited from a script is the actual tell in the room, so interview.py produces cues and
evidence rather than sentences to memorise. And it does not claim a text is
AI-detector-proof: detectors are unreliable in both directions, and anyone selling
certainty about that is selling something. What it does is remove the phrasings that make
a reader's eyes glaze, which is the part that actually costs you.
"""
from __future__ import annotations

import re

# ------------------------------------------------------------------------- the tells
# (pattern, what a person would write instead, why it reads as generated)
TELLS: list[tuple[str, str, str]] = [
    (r"\bdelve into\b", "look at", "nobody says delve out loud"),
    (r"\bleverage\b", "use", "consultancy filler"),
    (r"\bharness(?:ing)?\b", "use", "consultancy filler"),
    (r"\brobust\b", "reliable, or name the property", "vague intensifier"),
    (r"\bseamless(?:ly)?\b", "cut it", "vague intensifier"),
    (r"\bcutting[- ]edge\b", "name the actual technology", "brochure adjective"),
    (r"\bstate[- ]of[- ]the[- ]art\b", "name the actual technology", "brochure adjective"),
    (r"\bpassionate about\b", "say what you actually did about it",
     "the single most common opening in the pile"),
    (r"\bI am excited to\b", "I am applying because", "over-eager register"),
    (r"\bthrilled\b", "cut it", "over-eager register"),
    (r"\bin today'?s (?:fast[- ]paced|ever[- ]changing|competitive|dynamic)\b",
     "start with the specific thing instead", "essay-opening cliche"),
    (r"\bit is worth noting that\b", "cut it", "padding"),
    (r"\bfurthermore\b", "and, or start a new sentence", "essay connective"),
    (r"\bmoreover\b", "also", "essay connective"),
    (r"\bin conclusion\b", "cut it", "essay connective"),
    (r"\bmyriad\b", "many", "thesaurus reach"),
    (r"\bplethora\b", "plenty of", "thesaurus reach"),
    (r"\bmeticulous(?:ly)?\b", "careful, or show it", "self-praise adjective"),
    (r"\bdiligent(?:ly)?\b", "cut it", "self-praise adjective"),
    (r"\bcomprehensive\b", "full, or say what it covered", "vague intensifier"),
    (r"\bmultifaceted\b", "cut it", "vague intensifier"),
    (r"\btestament to\b", "cut it", "review-speak"),
    (r"\bunderscore(?:s|d)?\b", "shows", "review-speak"),
    (r"\bpivotal\b", "important, or say what it changed", "review-speak"),
    (r"\bembark(?:ed|ing)? on\b", "started", "grandiose verb"),
    (r"\bnavigat(?:e|ed|ing) the\b", "handled", "grandiose verb"),
    (r"\bfoster(?:ing)?\b", "build, or encourage", "grant-application verb"),
    (r"\bresonate(?:s|d)? with\b", "matters to me because", "vague"),
    (r"\bas an? (?:AI|language model)\b", "delete the whole sentence",
     "says out loud that it was generated"),
    (r"\bI hope this (?:email|message) finds you well\b",
     "open with why you are writing", "template greeting"),
    (r"\bdon'?t hesitate to\b", "feel free to, or just ask", "template closing"),
    (r"\bat the end of the day\b", "cut it", "filler"),
    (r"\bsynerg(?:y|ies|istic)\b", "cut it", "jargon"),
    (r"\bholistic\b", "cut it", "jargon"),
    (r"\bgame[- ]chang(?:er|ing)\b", "cut it", "hype"),
    (r"\bunparalleled\b", "cut it", "hype"),
    (r"\bever[- ]evolving\b", "cut it", "hype"),
    (r"\bkey takeaways?\b", "what I would do differently", "deck language"),
    (r"\bdeep dive\b", "look closely at", "deck language"),
    (r"\bimpactful\b", "say what changed", "not a real word to most readers"),
    (r"\butilis?e(?:d|s)?\b", "use", "longer word for no reason"),
]

_COMPILED = [(re.compile(pattern, re.I), better, why) for pattern, better, why in TELLS]

# Structural tells - shape rather than vocabulary.
_TRICOLON = re.compile(r"\b\w+,\s+\w+,\s+and\s+\w+\b")
_EM_DASH = re.compile(r"\s[—–]\s")
_NOT_ONLY = re.compile(r"\bnot (?:just|only)\b[^.]{0,80}\bbut\b", re.I)


def tells(text: str) -> list[dict]:
    """Every phrase that reads as generated, with what to put instead.

    Returned rather than fixed, because half of these need the writer to supply a fact
    the app does not have. "Say what changed" is advice a person can act on; a silent
    substitution would just hide the problem.
    """
    if not text:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for pattern, better, why in _COMPILED:
        match = pattern.search(text)
        if match and match.group(0).lower() not in seen:
            seen.add(match.group(0).lower())
            found.append({"phrase": match.group(0), "instead": better, "why": why,
                          "kind": "word"})

    if len(_EM_DASH.findall(text)) >= 3:
        found.append({"phrase": "em dashes", "instead": "use full stops for two of them",
                      "why": "three or more in a short text is a strong tell",
                      "kind": "shape"})
    if len(_TRICOLON.findall(text)) >= 2:
        found.append({"phrase": "lists of exactly three",
                      "instead": "make one of them two items, or four",
                      "why": "the rhythm repeats and starts to sound composed",
                      "kind": "shape"})
    if _NOT_ONLY.search(text):
        found.append({"phrase": "not only... but also",
                      "instead": "two plain sentences",
                      "why": "very heavily over-used in generated text", "kind": "shape"})
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if len(sentences) >= 5:
        lengths = [len(s.split()) for s in sentences]
        spread = max(lengths) - min(lengths)
        if spread <= 6 and sum(lengths) / len(lengths) > 14:
            found.append({"phrase": "every sentence the same length",
                          "instead": "put a four-word sentence in. Like this.",
                          "why": "uniform rhythm is what makes text feel flat",
                          "kind": "shape"})
    return found


def soften(text: str) -> str:
    """Mechanically swap the substitutions that are safe without knowing the facts."""
    safe = {
        r"\bleverage\b": "use", r"\bleveraging\b": "using",
        r"\butilise\b": "use", r"\butilize\b": "use",
        r"\butilised\b": "used", r"\butilized\b": "used",
        r"\bdelve into\b": "look at", r"\bmyriad\b": "many",
        r"\bplethora of\b": "plenty of", r"\bmoreover,?\s*": "",
        r"\bfurthermore,?\s*": "", r"\bin conclusion,?\s*": "",
        r"\bit is worth noting that\s*": "", r"\bembarked on\b": "started",
        r"\bI hope this email finds you well\.?\s*": "",
    }
    out = text or ""
    for pattern, replacement in safe.items():
        out = re.sub(pattern, replacement, out, flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip()


def score(text: str) -> int:
    """0-100, higher is more human. A rough gauge for the UI, not a verdict."""
    if not text or len(text.split()) < 20:
        return 100
    words = len(text.split())
    penalty = sum(9 if t["kind"] == "shape" else 6 for t in tells(text))
    # Long texts get a little slack: one tell in 600 words is not the same as one in 60.
    return max(0, min(100, 100 - int(penalty * max(0.4, min(1.6, 180 / words)))))


# ---------------------------------------------------------------- prompt instructions
# Given to the model verbatim. Concrete and testable rather than "write naturally",
# because "write naturally" is what produces the text this module exists to catch.
VOICE_RULES = """Write the way a person actually writes.

Hard rules:
- Never use: delve, leverage, harness, robust, seamless, cutting-edge, passionate about,
  thrilled, excited to, meticulous, comprehensive, multifaceted, testament to,
  underscores, pivotal, foster, resonate, myriad, plethora, impactful, utilise,
  synergy, holistic, game-changer, deep dive, key takeaways, unparalleled.
- Never open with "In today's fast-paced..." or any variant.
- Never write "not only X but also Y".
- No more than two em dashes in the whole text.
- Vary sentence length hard. Some sentences should be three words long.
- Prefer the short word. Use rather than utilise, shows rather than underscores.
- British spelling.

Content rules:
- Every claim needs a concrete noun attached. Not "strong analytical skills" but the
  thing that was analysed.
- If you do not know a fact, leave a square-bracket gap for the reader to fill:
  [name the project]. Never invent an achievement, a number or a company detail.
- No flattery of the employer and none of the candidate.
- Contractions are fine and make it read better."""
