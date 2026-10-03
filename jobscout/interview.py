"""Interview and assessment prep for one specific posting.

WHAT THIS IS BUILT ON
---------------------
Four sources, in descending order of reliability:

1. The posting itself. Employers say more than candidates notice - "online assessment",
   "take-home", "pair programming", "situational judgement", sometimes the platform by
   name. Parsed here rather than guessed.
2. Which applicant-tracking system the posting came from. A Workday req and a Greenhouse
   req imply different process shapes, and we already know which one this was.
3. A researched map of who uses which test provider. UK graduate hiring is concentrated:
   SHL across the banks, Cappfinity across the Big Four and the NHS, Watson Glaser across
   the law firms, HackerRank and CodeSignal across engineering. Those are real, checkable
   pairings, kept in PROVIDERS and EMPLOYER_PROVIDERS with the reasoning attached.
4. The employer's own careers and values pages, fetched politely and with robots.txt
   respected, because values-based interview questions come almost verbatim from them.

WHAT IT DELIBERATELY IS NOT
---------------------------
It is not a set of answers to memorise. Reciting a prepared paragraph is the thing that
loses interviews - it is audible, and it collapses the moment a follow-up question
arrives. So the brief gives you the question, why it is being asked, what the answer has
to contain, and which of your own projects is the evidence. You supply the sentences.

It also does not scrape Glassdoor. Their API closed to developers in 2022 and their terms
forbid automated collection, so the resource list deep-links their own search - the same
honest pattern the LinkedIn module uses for people.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from . import humanise
from .http import SESSION
from .models import Job

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("jobscout.interview")

MODEL = "claude-opus-5"


# ------------------------------------------------------------- assessment detection
# (label, what it is, regex over the posting, how to prepare)
ASSESSMENT_SIGNALS: list[tuple[str, str, str, str]] = [
    ("Coding assessment", "timed automated coding test",
     r"\b(coding (?:test|challenge|assessment|exercise)|technical (?:test|assessment)|"
     r"online assessment|oa\b|hackerrank|codility|codesignal|coderpad|leetcode|"
     r"kattis|devskiller)\b",
     "Practise the two or three patterns your language makes awkward, under a timer. "
     "Most of these mark on correctness and time complexity, not style."),
    ("Take-home task", "a piece of work done in your own time",
     r"\b(take[- ]home|technical task|practical exercise|case study submission|"
     r"portfolio (?:task|exercise))\b",
     "Budget half the time they suggest for the writing-up. A short README explaining "
     "what you would do with another week is what separates the top submissions."),
    ("System design", "design an architecture out loud",
     r"\b(system design|architecture (?:interview|discussion)|design review|scalab\w+)\b",
     "Practise drawing while talking. Start from the requirements and the read/write "
     "ratio, not from the database."),
    ("Pair programming", "write code with an interviewer watching",
     r"\b(pair(?:ed)? programming|live coding|collaborative coding)\b",
     "Narrate. Silence reads as being stuck even when you are not."),
    ("Numerical reasoning", "timed arithmetic and data interpretation",
     r"\b(numerical (?:reasoning|test)|numeric\w* assessment|data interpretation)\b",
     "The arithmetic is easy and the clock is not. Practise reading the chart before "
     "the question."),
    ("Verbal reasoning", "timed reading comprehension",
     r"\b(verbal reasoning|critical thinking test|watson[- ]glaser)\b",
     "The trick is answering only from the passage. Anything you know from outside it "
     "is a wrong answer."),
    ("Situational judgement", "ranked responses to workplace scenarios",
     r"\b(situational judge?ment|sjt\b|scenario[- ]based assessment)\b",
     "Answer as the company's stated values, not as yourself. This is the one test "
     "where reading their values page is directly worth marks."),
    ("Video interview", "recorded answers, no interviewer present",
     r"\b(video interview|one[- ]way interview|hirevue|sonru|shortlist\.me|"
     r"recorded (?:interview|responses))\b",
     "Record yourself once and watch it. Look at the lens, not the preview window, and "
     "use the practice question to fix your framing and levels."),
    ("Assessment centre", "a day of group and individual exercises",
     r"\b(assessment cent(?:re|er)|group exercise|ac\b|final stage day)\b",
     "The group exercise is marked on whether the group succeeds. Bringing a quiet "
     "person in scores; winning the argument does not."),
    ("Presentation", "prepared talk to a panel",
     r"\b(presentation (?:to|round|task)|present your findings|pitch)\b",
     "Time it out loud at least twice. Overrunning is the most common mark lost."),
    ("Competency interview", "structured questions on past behaviour",
     r"\b(competency[- ]based|behavioural interview|strengths[- ]based|star method)\b",
     "Six stories, each usable for three different questions, is better preparation "
     "than thirty stories."),
    ("Psychometric / personality", "untimed questionnaire",
     r"\b(personality (?:questionnaire|assessment)|psychometric|gamified assessment|"
     r"arctic shores|pymetrics)\b",
     "Answer consistently rather than strategically - these check for contradictions "
     "between similar questions."),
    ("Technical phone screen", "a recruiter or engineer on a call",
     r"\b(phone screen|technical screen|screening call|introductory call)\b",
     "Have a two-minute version of your background ready, and one question about the "
     "team that you actually want answered."),
]

_COMPILED_SIGNALS = [(label, kind, re.compile(pattern, re.I), how)
                     for label, kind, pattern, how in ASSESSMENT_SIGNALS]


# ------------------------------------------------------------------- test providers
# Real practice resources per provider. Free tiers noted, because that matters when you
# are applying to thirty places and nobody is paying for thirty practice packs.
PROVIDERS: dict[str, dict] = {
    "SHL": {
        "what": "Numerical, verbal, inductive reasoning and the Verify range. The most "
                "widely used family in UK graduate hiring.",
        "practice": [
            ("SHL's own practice tests", "https://www.shl.com/shldirect/en/practice-tests/",
             "free, and the closest thing to the real interface"),
            ("GraduatesFirst", "https://www.graduatesfirst.com/",
             "free practice tests aimed at UK graduate schemes"),
            ("AssessmentDay", "https://www.assessmentday.co.uk/shl.htm",
             "free samples, paid packs"),
        ],
    },
    "Cappfinity": {
        "what": "Strengths-based and immersive assessments. Scenario-heavy rather than "
                "arithmetic-heavy, and scored on what energises you, not only accuracy.",
        "practice": [
            ("GraduatesFirst strengths practice", "https://www.graduatesfirst.com/",
             "free"),
            ("Practice Aptitude Tests - Cappfinity", "https://www.practiceaptitudetests.com/testing-publishers/cappfinity/",
             "free samples"),
        ],
    },
    "Korn Ferry / Talent Q": {
        "what": "Adaptive questions - each answer changes the next one's difficulty, so "
                "the test gets harder when you are doing well. That is normal.",
        "practice": [
            ("Practice Aptitude Tests", "https://www.practiceaptitudetests.com/testing-publishers/talent-q/",
             "free samples"),
            ("AssessmentDay Talent Q", "https://www.assessmentday.co.uk/talent-q.htm",
             "free samples"),
        ],
    },
    "Watson Glaser": {
        "what": "Critical thinking. The law-firm standard. Five sections including "
                "inference and recognition of assumptions.",
        "practice": [
            ("Practice Aptitude Tests", "https://www.practiceaptitudetests.com/testing-publishers/watson-glaser/",
             "free samples"),
            ("AssessmentDay Watson Glaser",
             "https://www.assessmentday.co.uk/watson-glaser-critical-thinking.htm",
             "free samples"),
        ],
    },
    "Aon / cut-e": {
        "what": "Short, very fast sub-tests. Speed matters more than on most batteries.",
        "practice": [
            ("Practice Aptitude Tests", "https://www.practiceaptitudetests.com/testing-publishers/cut-e/",
             "free samples"),
        ],
    },
    "HireVue": {
        "what": "One-way recorded video. A question appears, you get thinking time, then "
                "it records. Usually no retakes.",
        "practice": [
            ("GraduatesFirst video interview practice",
             "https://www.graduatesfirst.com/", "free, and it covers HireVue by name"),
            ("Your phone's camera", "", "genuinely the highest-value practice available"),
        ],
    },
    "Arctic Shores": {
        "what": "Gamified, task-based. There is no right answer to optimise, which is the "
                "point - it measures how you behave rather than what you know.",
        "practice": [
            ("Arctic Shores candidate guide", "https://www.arcticshores.com/candidate-guidance",
             "free"),
        ],
    },
    "HackerRank": {
        "what": "Automated coding tests, employer-configured. Often includes multiple "
                "choice on language details alongside the problems.",
        "practice": [
            ("HackerRank Interview Preparation Kit",
             "https://www.hackerrank.com/interview/interview-preparation-kit", "free"),
            ("NeetCode", "https://neetcode.io/", "free, pattern-first"),
        ],
    },
    "CodeSignal": {
        "what": "Often a standardised certified assessment scored 200-600, reusable "
                "across employers. Four questions, strict clock.",
        "practice": [
            ("CodeSignal practice", "https://codesignal.com/", "free tier"),
            ("NeetCode 150", "https://neetcode.io/practice", "free"),
        ],
    },
    "Codility": {
        "what": "Weights correctness and performance heavily - a solution that passes but "
                "times out on large inputs scores badly.",
        "practice": [
            ("Codility training", "https://app.codility.com/programmers/lessons/",
             "free lessons, and their own past tasks"),
        ],
    },
    "CoderPad": {
        "what": "A shared editor with a human on the call. Collaborative, not automated.",
        "practice": [
            ("CoderPad sandbox", "https://coderpad.io/", "free"),
            ("interviewing.io", "https://interviewing.io/",
             "anonymous mock interviews with engineers"),
        ],
    },
}

# Which employers use which, from published candidate reports and the providers' own
# case studies. Matched on whole words against the company name; a miss just means the
# brief says "not known", which is better than a confident guess.
EMPLOYER_PROVIDERS: list[tuple[list[str], list[str], str]] = [
    (["goldman sachs"], ["HackerRank", "SHL", "HireVue"],
     "HackerRank for engineering, SHL for the wider scheme"),
    (["barclays", "deutsche bank", "pwc", "natwest", "lloyds", "santander",
      "nationwide", "aviva", "legal & general"], ["SHL"],
     "SHL is the standard battery across UK banking and insurance graduate hiring"),
    (["hsbc", "deloitte", "ey", "ernst & young", "kpmg", "nhs"], ["Cappfinity"],
     "Cappfinity immersive assessments"),
    (["linklaters", "clifford chance", "freshfields", "allen & overy", "a&o shearman",
      "slaughter and may", "hogan lovells", "herbert smith freehills"],
     ["Watson Glaser"],
     "Watson Glaser critical thinking is the law-firm standard"),
    (["civil service", "fast stream", "gov.uk", "cabinet office"],
     ["SHL", "Watson Glaser"],
     "Fast Stream runs numerical and verbal tests, a situational judgement test and an "
     "e-tray exercise"),
    (["unilever", "vodafone"], ["HireVue", "Arctic Shores"],
     "gamified assessment then a one-way video"),
    (["capital one", "microsoft", "lseg", "figma", "robinhood", "brex"],
     ["CodeSignal", "HackerRank"],
     "reported by candidates for recent engineering intakes"),
    (["swift"], ["Codility"], "Codility for engineering screens"),
    (["monday.com"], ["CoderPad"], "live collaborative coding"),
]


def providers_for(company: str, description: str = "") -> list[dict]:
    """Which assessment providers this employer is likely to use, and why we think so."""
    name = f" {(company or '').lower()} "
    text = (description or "").lower()
    found: dict[str, str] = {}

    for needles, provider_names, reason in EMPLOYER_PROVIDERS:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", name)
               for n in needles):
            for provider in provider_names:
                found.setdefault(provider, f"typical for this employer - {reason}")

    # Named in the posting beats any mapping.
    for provider in PROVIDERS:
        token = provider.split(" /")[0].lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            found[provider] = "named in the posting itself"

    return [{"name": name_, "why": why, **PROVIDERS[name_]}
            for name_, why in found.items() if name_ in PROVIDERS]


# Employer-specific practice packs, keyed by slug. Built offline by
# tools/discover_assessments.py from AssessmentDay's sitemap, so no request is made
# while a brief is being assembled.
_PACKS: dict | None = None


def _packs() -> dict:
    global _PACKS
    if _PACKS is None:
        import yaml
        path = ROOT / "data" / "assessment_profiles.yaml"
        try:
            _PACKS = (yaml.safe_load(path.read_text("utf-8")) or {}).get("employers", {})
        except OSError:
            log.info("no assessment_profiles.yaml; run tools/discover_assessments.py")
            _PACKS = {}
    return _PACKS


def _slug(name: str) -> str:
    """Company name to the slug shape AssessmentDay uses in its URLs."""
    text = re.sub(r"\b(group|plc|ltd|limited|llp|inc|uk|holdings|company|the)\b", " ",
                  (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def employer_packs(company: str) -> list[dict]:
    """Practice papers published for this exact employer, if any exist.

    The highest-value thing in the brief when it hits: practice in the same format the
    employer will actually send, rather than a generic numerical-reasoning paper.
    """
    packs = _packs()
    if not packs or not company:
        return []
    slug = _slug(company)
    entry = packs.get(slug)
    if not entry:
        # "Rolls-Royce Motor Cars" should still find "rolls-royce".
        for key, value in packs.items():
            if len(key) > 3 and (slug.startswith(key + "-") or key.startswith(slug + "-")):
                entry = value
                break
    if not entry:
        return []
    return [{"test": t["test"], "url": t["url"], "company": entry["company"]}
            for t in entry.get("tests", [])]


def detect_assessments(job: Job) -> list[dict]:
    """Stages this posting actually mentions."""
    text = f"{job.title}\n{job.description}"
    found = []
    for label, kind, pattern, how in _COMPILED_SIGNALS:
        match = pattern.search(text)
        if match:
            found.append({"stage": label, "what": kind, "prepare": how,
                          "evidence": f"the posting says “{match.group(0)}”"})
    return found


# --------------------------------------------------------------------- process shape
# What a process usually looks like when the posting does not spell it out. Keyed by
# what we can actually observe: the tracking system and the kind of role.
TYPICAL_PROCESS = {
    "graduate_scheme": ["Application form", "Online tests", "Video interview",
                        "Assessment centre", "Final interview"],
    "placement": ["Application form", "Online tests or a short task",
                  "Video or phone interview", "Interview with the team"],
    "internship": ["Application form", "Online test or take-home",
                   "Technical interview", "Final interview"],
    "apprenticeship": ["Application form", "Online tests", "Telephone interview",
                       "Assessment day"],
    "full_time": ["Application", "Recruiter screen", "Technical or task stage",
                  "Team interview", "Final conversation"],
    "part_time": ["Application", "Phone screen or trial shift", "Interview with the manager"],
    "seasonal": ["Application", "Group interview or trial shift"],
    "zero_hours": ["Application", "Short interview with the manager"],
}


def likely_process(job: Job, kind: str) -> list[str]:
    """Best guess at the stages, labelled as a guess where it is one."""
    stages = list(TYPICAL_PROCESS.get(kind or "", []) or TYPICAL_PROCESS["full_time"])
    # Substring, not equality: the templates spell it "Online tests", "Online test or a
    # short task" and "Online test or take-home", and adding a fourth wording next to
    # any of them reads as two separate test stages.
    already = any("online test" in stage.lower() for stage in stages)
    if "workday" in f"{job.source} {job.url}".lower() and not already:
        # Workday employers are large by definition, and large employers screen with an
        # automated stage before a person reads anything.
        stages.insert(1, "Online test (large employers usually add one)")
    return stages


# ------------------------------------------------------------------ company values
# Ordered by how often each guess actually resolves on real employer sites (checked
# against a sample of UK career pages): "careers" and the two "about" spellings account
# for most hits, so they run first. That matters because of _VALUES_BUDGET_SECONDS below
# - if the clock runs out partway through, the paths most likely to pay off have already
# had their turn.
VALUE_PAGES = ["careers", "about-us", "about", "culture", "our-values", "values",
               "who-we-are", "working-here", "life-at-us"]

_VALUES_BLOCK = re.compile(
    r"(our values|our purpose|what we (?:believe|stand for)|how we work|"
    r"our principles|the way we work)(.{80,2200})", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
# Short, title-cased or imperative phrases - which is how every values page writes them.
_VALUE_PHRASE = re.compile(r"^[A-Z][A-Za-z' ]{3,44}$")

# Words that look like values but are navigation furniture.
_NOT_A_VALUE = {"cookie policy", "privacy policy", "terms of use", "contact us",
                "search jobs", "sign in", "read more", "learn more", "our people",
                "our story", "modern slavery statement", "accessibility"}


# How long the whole probe is allowed to run, wall-clock, regardless of how many of
# VALUE_PAGES are left to try. Nine sequential guesses at one retry and an 8s timeout
# each could still add up to over a minute against a genuinely unreachable host; this
# caps it so one slow employer website can never stall an interview-brief request by
# more than a few seconds beyond what an unresponsive site already costs on try one.
_VALUES_BUDGET_SECONDS = 8.0


def company_values(company: str, website: str = "") -> dict:
    """Read the employer's stated values off their own site.

    Worth the fetch because values-based and situational-judgement questions are written
    from this page, often word for word. Politely, robots-respecting, and cached by the
    shared session; a failure returns empty rather than raising.

    Most of the nine guessed paths do not exist for most employers - that is the normal
    case, not a transient error - so this asks for one attempt each with a short timeout
    rather than the shared session's default three retries, and caches a 404 so a repeat
    request for the same employer is served from disk instead of hitting their site
    again. On the first call for a new employer this brings the typical cost down from
    several seconds (nine sequential throttled fetches) to well under one; on every call
    after, it is instant.
    """
    if not website:
        return {"values": [], "source": "", "note": "No company website known."}
    base = website.rstrip("/")
    started = time.monotonic()
    for path in VALUE_PAGES:
        if time.monotonic() - started > _VALUES_BUDGET_SECONDS:
            log.info("company_values: time budget spent on %s, stopping early", company)
            break
        url = f"{base}/{path}"
        try:
            html = SESSION.get_text(url, retries=1, timeout=5, cache_misses=True)
        except Exception as exc:
            log.debug("values fetch failed for %s: %s", url, exc)
            continue
        if not html or len(html) < 400:
            continue
        match = _VALUES_BLOCK.search(html)
        if not match:
            continue
        block = _TAG.sub("\n", match.group(2))
        candidates = []
        for line in block.splitlines():
            line = re.sub(r"\s{2,}", " ", line).strip(" .•-–")
            if (_VALUE_PHRASE.match(line) and line.lower() not in _NOT_A_VALUE
                    and line not in candidates):
                candidates.append(line)
        if len(candidates) >= 2:
            return {"values": candidates[:8], "source": url,
                    "note": "Read from the employer's own page. Check it yourself before "
                            "quoting anything back at them."}
    return {"values": [], "source": "",
            "note": "Could not find a values page automatically. Worth two minutes on "
                    "their careers site - situational judgement tests are marked "
                    "against exactly this."}


# ------------------------------------------------------------------- question banks
# Behavioural questions, with what the answer has to contain. The "needs" line is the
# useful half: most people know the question and still answer it without the evidence.
BEHAVIOURAL = [
    ("Tell me about yourself.",
     "Ninety seconds. Where you are now, one thing you have built, why this role. Not "
     "your life story and not your CV read aloud."),
    ("Why this company, and not the one next door?",
     "One specific thing about their work that you could not say about a competitor. "
     "Having read a product page counts; having tried the product counts more."),
    ("Why this role?",
     "Connect it to something you have already chosen to do unpaid or in your own time."),
    ("Tell me about a time something you were working on went wrong.",
     "What you did next, and what you changed afterwards. The failure is the setup, not "
     "the answer."),
    ("Give me an example of working in a team where someone was not pulling their weight.",
     "What you said to them. Answers that solve it by doing their work too score badly."),
    ("Describe a time you had to learn something quickly.",
     "Name the thing, how long you had, and how you knew you had got it right."),
    ("When have you had to change your mind about something?",
     "What the evidence was. This tests whether you update, not whether you are humble."),
    ("Tell me about a deadline you nearly missed.",
     "What you cut. Prioritisation is what is being marked."),
    ("What are you worse at than you would like to be?",
     "A real one, with what you are doing about it. 'Perfectionism' is heard forty times "
     "a day and scores nothing."),
    ("What questions do you have for us?",
     "Two, written down, that you actually want answered. 'What does a good first six "
     "months look like?' works everywhere."),
]

# Technical ground by field of work. The topics are what interviewers reach for first.
TECHNICAL_TOPICS: dict[str, list[tuple[str, str]]] = {
    "software": [
        ("Data structures and complexity",
         "Arrays, hash maps, trees. Be able to say why you chose one and what it costs."),
        ("A language you will be judged on",
         "Know its awkward parts: mutability, equality, async, memory."),
        ("Debugging out loud",
         "How you would find a bug you cannot reproduce. Asked more often than sorting."),
        ("Version control and review",
         "What you do when you break the build. Junior roles ask this constantly."),
        ("Testing", "What you would test first, and what you would not bother testing."),
    ],
    "data": [
        ("SQL joins, grouping and window functions",
         "Live SQL is the single most common data screen."),
        ("Cleaning and missing data",
         "What you do with nulls, and why dropping rows is usually wrong."),
        ("Statistics you will be asked to defend",
         "Sampling, significance, correlation against causation, and A/B basics."),
        ("Explaining a result to someone non-technical",
         "Usually the second half of the interview and often the deciding half."),
        ("A visualisation you built",
         "Have one you can describe, including what you chose not to plot."),
    ],
    "engineering": [
        ("First-principles calculation",
         "Expect to estimate something on paper: loads, flow, power, tolerance."),
        ("A design you took from brief to hardware",
         "Constraints, the trade-off you made, what failed in testing."),
        ("CAD and drawings",
         "Be ready to talk tolerances and GD&T if the role touches manufacturing."),
        ("Standards and safety",
         "Which standard applied to your project, and how you knew."),
        ("Materials and process choice",
         "Why that material, why that process, what it cost."),
    ],
    "science": [
        ("Your lab technique end to end",
         "Sample preparation, controls, what could invalidate the result."),
        ("Data handling and error",
         "Uncertainty, repeats, and what you would do about an outlier."),
        ("Reading a paper critically",
         "Often given one on the day and asked what is wrong with it."),
        ("Good documentation practice",
         "Lab notebooks, traceability, why it matters in a regulated setting."),
    ],
    "finance": [
        ("The three statements and how they connect",
         "Asked in some form in almost every finance interview."),
        ("A valuation you can walk through",
         "DCF or comparables, and what the answer is most sensitive to."),
        ("A market or company you follow",
         "One view, held with a reason and a number attached."),
        ("Excel under observation",
         "Lookups, pivots, and building something without touching the mouse."),
    ],
    "retail": [
        ("A difficult customer, handled",
         "What you said. This is the whole interview in most retail roles."),
        ("Working a rush",
         "How you prioritised when three things needed doing at once."),
        ("Availability and reliability",
         "Be precise and honest about hours. Getting this wrong ends it later."),
        ("Why this shop",
         "Having shopped there and noticing something is enough, and it works."),
    ],
    "hospitality": [
        ("Pace and pressure",
         "A busy shift you got through, and what you did when an order went wrong."),
        ("Food safety and hygiene basics",
         "Allergens especially. Any awareness at all puts you ahead."),
        ("Upselling without being annoying",
         "A real example, even from another job."),
    ],
    "care": [
        ("Dignity and consent in practice",
         "A concrete situation, not a definition."),
        ("Safeguarding",
         "What you would do if you were worried about someone. Know the escalation route."),
        ("Handling distress",
         "What you said, how you stayed calm, who you told afterwards."),
    ],
    "customer_service": [
        ("De-escalating an angry caller",
         "The actual words you would use."),
        ("Following a process you disagree with",
         "How you raised it and what you did meanwhile."),
    ],
    "operations": [
        ("A process you improved",
         "The measurement before and after. Any number at all helps."),
        ("Prioritising competing demands",
         "Your rule for deciding, not just the story."),
    ],
    "driving": [
        ("Licence, points and vehicle categories",
         "Know your own details exactly."),
        ("A delay or breakdown you handled",
         "Who you told, and how fast."),
        ("Hours rules and safety",
         "Tachograph and break rules if it is HGV work."),
    ],
    "marketing": [
        ("A campaign you can pull apart",
         "One you admire, one you think failed, and why."),
        ("Measurement", "Which metric you would hold yourself to and why."),
    ],
    "sales": [
        ("A time you changed someone's mind",
         "Any context. Selling is the transferable part."),
        ("Handling a no", "What you did next."),
    ],
}


def values_questions(values: list[str], company: str) -> list[dict]:
    """Turn stated values into the questions they get turned into.

    This is the most directly useful part of the brief for situational judgement tests
    and values interviews, because the mapping really is this mechanical.
    """
    questions = []
    for value in values[:5]:
        clean = value.rstrip(".").strip()
        questions.append({
            "question": f"Give me an example of when you showed “{clean}”.",
            "kind": "values",
            "needs": f"{company} lists this on their own site, so it will be scored. "
                     f"Have one story ready that demonstrates it without using the "
                     f"phrase itself.",
        })
    return questions


# ---------------------------------------------------------------------- resources
def resources(company: str, family: str, is_technical: bool) -> list[dict]:
    """Prep resources, including the ones other candidates actually use.

    Company-specific links go to each site's own search rather than a guessed URL, which
    keeps them working when the sites restructure - and means we are linking, not
    harvesting someone else's content.
    """
    quoted = quote_plus(company or "")
    items: list[dict] = [
        {"name": "Glassdoor interview reports",
         "url": f"https://www.glassdoor.co.uk/Search/results.htm?keyword={quoted}",
         "what": "Candidates post the actual questions and the stage they were asked at. "
                 "The most valuable single source for a named employer.",
         "cost": "free account"},
        {"name": "Reddit and student forums",
         "url": f"https://www.google.com/search?q={quoted}+interview+"
                f"site%3Areddit.com+OR+site%3Athestudentroom.co.uk",
         "what": "Where people post the assessment they just sat, often the same week.",
         "cost": "free"},
        {"name": "The employer's own careers site",
         "url": f"https://www.google.com/search?q={quoted}+careers+interview+process",
         "what": "Many publish the stages, and some publish the questions. Read it before "
                 "anything else.",
         "cost": "free"},
    ]

    if is_technical:
        items += [
            {"name": "Tech Interview Handbook",
             "url": "https://www.techinterviewhandbook.org/",
             "what": "Free, curated, and short enough to finish. Start with the Grind 75 "
                     "list rather than grinding at random.",
             "cost": "free"},
            {"name": "NeetCode",
             "url": "https://neetcode.io/practice",
             "what": "Patterns first, with worked video explanations. NeetCode 150 covers "
                     "what most screens draw from.",
             "cost": "free tier"},
            {"name": "LeetCode",
             "url": "https://leetcode.com/problemset/all/",
             "what": "The problem bank itself. Filter by topic rather than by company - "
                     "company tags are behind the paid tier.",
             "cost": "free tier"},
            {"name": "interviewing.io",
             "url": "https://interviewing.io/",
             "what": "Anonymous mock interviews with real engineers, and recordings of "
                     "other people's. Watching one is worth ten solved problems.",
             "cost": "free tier"},
        ]
    if family == "software":
        items.append({"name": "System Design Primer",
                      "url": "https://github.com/donnemartin/system-design-primer",
                      "what": "The standard free reference for design rounds.",
                      "cost": "free"})
    if family == "data":
        items += [
            {"name": "DataLemur", "url": "https://datalemur.com/",
             "what": "SQL and statistics questions from real data interviews.",
             "cost": "free tier"},
            {"name": "StrataScratch", "url": "https://www.stratascratch.com/",
             "what": "SQL and Python questions taken from company screens.",
             "cost": "free tier"},
        ]

    items += [
        {"name": "GraduatesFirst",
         "url": "https://www.graduatesfirst.com/",
         "what": "Free practice aptitude tests, video interviews and gamified "
                 "assessments, aimed specifically at UK graduate schemes.",
         "cost": "free tier"},
        {"name": "AssessmentDay",
         "url": "https://www.assessmentday.co.uk/",
         "what": "Practice papers by publisher - SHL, Talent Q, Watson Glaser. Free "
                 "samples of each.",
         "cost": "free samples"},
        {"name": "Practice Aptitude Tests",
         "url": "https://www.practiceaptitudetests.com/",
         "what": "Publisher-specific practice plus employer-specific guides.",
         "cost": "free tier"},
    ]
    return items


# ------------------------------------------------------------------------- the brief
@dataclass
class InterviewBrief:
    job_id: str
    title: str
    company: str
    stages: list[dict] = field(default_factory=list)
    likely_stages: list[str] = field(default_factory=list)
    providers: list[dict] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    values_source: str = ""
    values_note: str = ""
    questions: list[dict] = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)
    your_evidence: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    employer_packs: list[dict] = field(default_factory=list)
    opening_lines: list[str] = field(default_factory=list)
    engine: str = "researched"
    voice: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _evidence_map(job: Job, profile) -> list[dict]:
    """Pair what the posting asks for with what the candidate has actually done.

    The most useful page in the whole brief, and the least clever: for each skill the
    posting names and the profile also has, say which question it is the answer to.
    """
    if not profile or not profile.skills:
        return []
    wanted = set(job.matched_skills or [])
    if not wanted:
        from .cv import extract_skills
        wanted = set(extract_skills(job.description)) & set(profile.skills)

    rows = []
    for skill in list(wanted)[:8]:
        rows.append({
            "skill": skill,
            "use_for": f"any question about {skill}, and as the concrete detail in "
                       f"“why this role”",
            "prepare": f"One example where you used {skill} and something went wrong or "
                       f"changed. Know the numbers - size, duration, result.",
        })
    gaps = [s for s in (job.missing_skills or []) if s][:4]
    for skill in gaps:
        rows.append({
            "skill": skill,
            "use_for": "the gap they will probe",
            "prepare": f"You have not shown {skill}. Say so plainly, then say the "
                       f"nearest thing you have done and how long you think it would "
                       f"take to pick up. Bluffing this is how interviews end.",
            "gap": True,
        })
    return rows


def _opening_lines(job: Job, profile, values: list[str]) -> list[str]:
    """Answers to 'why us' that are specific enough to be worth saying.

    Templates with the gaps left visible, because a filled-in-for-you answer is the one
    that sounds rehearsed. The bracket is the point.
    """
    lines = []
    if values:
        lines.append(
            f"They put “{values[0]}” on their own careers page. If you have "
            f"a story that shows it, that is your answer to “why us” - "
            f"[which of yours?].")
    subject = getattr(profile, "field_of_study", "") if profile else ""
    if subject:
        lines.append(
            f"Link the {subject} side of your degree to one thing in the posting: "
            f"“the part of {subject.lower()} I kept choosing was [X], and this role "
            f"is mostly [X].”")
    if job.location:
        lines.append(f"If asked about {job.location}: have a real reason, even a boring "
                     f"one. “I can be there in 40 minutes” is a good answer.")
    lines.append("Have one question that shows you read past the advert. "
                 "“The posting mentions [detail] - is that new?”")
    return lines


# -------------------------------------------------------------------------- model
def _claude_questions(job: Job, profile, values: list[str],
                      stages: list[dict]) -> tuple[list[dict], list[str], str] | None:
    """Ask Claude for questions specific to this posting, in a human voice.

    The posting text is sent; nothing about the user is, beyond the skills and course
    already public on their LinkedIn. Returns None when no key is configured, and the
    researched question bank is used instead - the app is fully usable without a key.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; using the researched question bank")
        return None

    tool = {
        "name": "interview_brief",
        "description": "Questions this specific employer is likely to ask, and why.",
        "input_schema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "kind": {"type": "string",
                                     "enum": ["technical", "behavioural", "values",
                                              "role", "your_turn"]},
                            "needs": {"type": "string",
                                      "description": "What the answer must contain. "
                                                     "Not the answer itself."},
                        },
                        "required": ["question", "kind", "needs"],
                    },
                },
                "topics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"topic": {"type": "string"},
                                       "why": {"type": "string"}},
                        "required": ["topic", "why"],
                    },
                },
                "caveats": {"type": "array", "items": {"type": "string"},
                            "description": "Anything uncertain about this employer's "
                                           "process, said plainly."},
            },
            "required": ["questions", "topics"],
        },
    }

    prompt = f"""Prepare interview questions for one specific job advert.

ROLE: {job.title}
EMPLOYER: {job.company}
LOCATION: {job.location or "not stated"}
STAGES THE ADVERT MENTIONS: {", ".join(s["stage"] for s in stages) or "none stated"}
THE EMPLOYER'S STATED VALUES: {", ".join(values) or "not found"}
CANDIDATE'S COURSE: {getattr(profile, "field_of_study", "") or "unknown"} \
({getattr(profile, "degree", "") or "unknown level"})
CANDIDATE'S SKILLS: {", ".join((getattr(profile, "skills", None) or [])[:20]) or "unknown"}

THE ADVERT:
{job.description[:5000]}

Give 8 to 14 questions that THIS employer would plausibly ask for THIS role - drawn from
the advert's own wording, its stated responsibilities and the stated values. Include at
least two technical questions specific to the work described, not generic ones. Include
one question the candidate should ask them.

For each, "needs" says what a good answer must contain - a fact, a number, a decision.
Never write the answer.

Then list the technical topics to revise, in the order you would revise them.

{humanise.VOICE_RULES}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL, max_tokens=3000,
            tools=[tool], tool_choice={"type": "tool", "name": "interview_brief"},
            messages=[{"role": "user", "content": prompt}])
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                data = block.input
                questions = [q for q in data.get("questions", []) if q.get("question")]
                topics = [{"topic": t["topic"], "why": t["why"]}
                          for t in data.get("topics", []) if t.get("topic")]
                return questions, topics, "\n".join(
                    q["question"] + " " + q.get("needs", "") for q in questions)
    except Exception as exc:
        log.warning("Claude unavailable for the interview brief: %s", exc)
    return None


# ----------------------------------------------------------------------- assembly
def build_brief(job: Job, profile=None, company_info: dict | None = None,
                kind: str = "", family: str = "", use_model: bool = True) -> InterviewBrief:
    """Everything worth knowing before an interview for this posting."""
    stages = detect_assessments(job)
    provider_list = providers_for(job.company, job.description)

    website = (company_info or {}).get("website") or ""
    found_values = company_values(job.company, website)
    values = found_values["values"]

    is_technical = family in ("software", "data", "engineering", "science") or bool(
        re.search(r"\b(engineer|developer|analyst|scientist|technician|architect)\b",
                  job.title, re.I))

    brief = InterviewBrief(
        job_id=job.id, title=job.title, company=job.company,
        stages=stages,
        likely_stages=likely_process(job, kind),
        providers=provider_list,
        values=values,
        values_source=found_values["source"],
        values_note=found_values["note"],
        topics=[{"topic": t, "why": w}
                for t, w in TECHNICAL_TOPICS.get(family, [])],
        your_evidence=_evidence_map(job, profile),
        resources=resources(job.company, family, is_technical),
        employer_packs=employer_packs(job.company),
        opening_lines=_opening_lines(job, profile, values),
    )

    questions = [{"question": q, "kind": "behavioural", "needs": needs}
                 for q, needs in BEHAVIOURAL]
    questions = values_questions(values, job.company) + questions

    generated = _claude_questions(job, profile, values, stages) if use_model else None
    if generated:
        model_questions, model_topics, text = generated
        # Model questions first: they are specific to this advert, the bank is general.
        brief.questions = model_questions + questions[:6]
        if model_topics:
            brief.topics = model_topics + brief.topics
        brief.engine = MODEL
        brief.voice = {"score": humanise.score(text), "tells": humanise.tells(text)[:6]}
    else:
        brief.questions = questions
        brief.engine = "researched question bank"
        brief.voice = {"score": 100, "tells": []}

    if not brief.topics:
        brief.topics = [{"topic": t, "why": w}
                        for t, w in TECHNICAL_TOPICS.get("operations", [])]

    brief.caveats = [
        "Stages marked “likely” are inferred from the kind of role, not stated "
        "by this employer. Check their careers page.",
        "Do not memorise answers. Every question here comes with what the answer needs, "
        "so you can say it differently each time and still say the right thing.",
    ]
    if not stages:
        brief.caveats.append(
            "This advert names no assessment at all, which usually means a normal "
            "interview - but large employers add an online test without mentioning it.")
    if not provider_list:
        brief.caveats.append(
            "No test provider is known for this employer. If you get an email with a "
            "test link, the provider's name is in the URL, and the practice resources "
            "below are organised by provider.")
    return brief
