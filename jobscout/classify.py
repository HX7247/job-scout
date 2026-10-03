"""Work out what kind of job a posting actually is.

Two independent questions, because they are genuinely independent - a barista job can
be part-time or full-time, and a software job can be an internship or permanent:

  employment kind   how much of your week it takes and on what basis
                    (part-time, full-time, internship, placement, apprenticeship,
                     contract, temporary, weekend, zero-hours, seasonal, volunteer)

  job family        what the work is
                    (retail, hospitality, engineering, software, healthcare, ...)

Sources disagree wildly on the employment field - the same database holds "FullTime",
"Full-Time", "Full Time", "full_time", "Mid, fulltime permanent" and "Employee -
Permanent" - and two thirds of postings leave it blank. So the raw field is only a
hint; the title and description decide.

Nothing here guesses when there is no evidence. A posting that says nothing about
hours returns "" rather than a made-up "full_time", so the UI can be honest about it.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- employment kind
# Ordered: the first kind whose signals appear wins, so "summer internship" is an
# internship rather than being read as temporary/seasonal.
EMPLOYMENT_KINDS: list[tuple[str, str, list[str]]] = [
    ("placement", "Placement / year in industry", [
        "industrial placement", "year in industry", "placement year", "sandwich year",
        "placement student", "12 month placement", "12-month placement",
        "undergraduate placement", "student placement", "industrial year",
        "work placement", "summer placement", "placement programme",
        "placement scheme",
    ]),
    ("internship", "Internship", [
        "internship", "intern,", "intern ", "summer intern", "winter intern",
        "vacation scheme", "co-op", "co op programme", "stage ", "praktikum",
        "estágio", "becario", "tirocinio", "work experience",
    ]),
    ("apprenticeship", "Apprenticeship", [
        "apprentice", "apprenticeship", "degree apprenticeship", "traineeship",
        "ausbildung", "alternance", "school leaver programme",
    ]),
    ("graduate_scheme", "Graduate scheme / role", [
        "graduate scheme", "graduate programme", "graduate program",
        "graduate development", "rotational programme", "rotational program",
        "graduate trainee",
        # US/global wording for the same thing, and the GitHub trackers' "New grad".
        "new grad", "new graduate", "graduate role", "graduate job", "graduate position",
        "graduate engineer", "graduate analyst",
    ]),
    ("weekend", "Weekend", [
        "weekend", "saturday only", "sunday only", "weekends only",
    ]),
    ("zero_hours", "Zero hours / flexible", [
        "zero hours", "zero-hour", "0 hours", "casual worker", "as and when",
        "flexible hours contract", "bank staff", "on-call staff",
    ]),
    ("part_time", "Part-time", [
        "part time", "part-time", "parttime", "p/t", "20 hours", "16 hours",
        "hours per week", "hrs per week", "evenings", "term time", "term-time",
        "teilzeit", "temps partiel", "media jornada",
    ]),
    ("seasonal", "Seasonal / temporary", [
        "seasonal", "christmas temp", "summer temp", "holiday cover",
        "peak season", "fixed term", "fixed-term", "ftc", "maternity cover",
        "temporary", "temp ",
    ]),
    ("contract", "Contract / freelance", [
        "contract", "contractor", "freelance", "consultant day rate", "outside ir35",
        "inside ir35", "b2b contract", "interim",
    ]),
    ("volunteer", "Voluntary", [
        "volunteer", "voluntary", "unpaid role", "charity volunteer",
    ]),
    ("full_time", "Full-time", [
        "full time", "full-time", "fulltime", "permanent", "37.5 hours",
        "40 hours", "vollzeit", "temps plein", "jornada completa",
    ]),
]

_KIND_LABELS = {key: label for key, label, _ in EMPLOYMENT_KINDS}
EMPLOYMENT_ORDER = [key for key, _, _ in EMPLOYMENT_KINDS]


def employment_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, "Not stated")



def _compile(signals: list[str]) -> "re.Pattern":
    """Word-boundary matcher for a signal list.

    Naive substring matching is wrong here and was actively harmful: "pub" matched
    inside "Global Public Sector", tagging a machine-learning role as hospitality.
    Signals may contain spaces, punctuation and accents, so the boundary is
    "not a letter or digit" rather than \b, which would misbehave around "c++".
    """
    parts = sorted((re.escape(sig.strip()) for sig in signals if sig.strip()),
                   key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


def _normalise_raw(raw: str) -> str:
    """Squash a source's employment field into something matchable."""
    text = (raw or "").lower()
    text = re.sub(r"[_\-/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_KIND_PATTERNS = [(key, _compile(sigs)) for key, _, sigs in EMPLOYMENT_KINDS]

# Words that mean something different in prose than in a title. A description
# mentioning an on-call "weekend" rota, "evenings" or a "contract of employment"
# does not make the job weekend, evening or contract work - one such description
# had a full-time infrastructure role classified as weekend work.
# An allowlist, not a blocklist: a blocklist kept leaking. A description saying
# "fixed term" or "seasonal" is usually describing the company's other contracts,
# not this job, and that tagged a permanent recruiter role as seasonal. Only
# phrases that state this job's own basis unambiguously are trusted in prose.
_PROSE_SAFE = {
    "part time", "part-time", "parttime", "full time", "full-time", "fulltime",
    "zero hours", "zero-hour", "apprenticeship", "internship", "term time",
    "term-time", "teilzeit", "vollzeit",
}
_KIND_PROSE_PATTERNS = [
    (key, _compile([s for s in sigs if s in _PROSE_SAFE]))
    for key, _, sigs in EMPLOYMENT_KINDS
    if [s for s in sigs if s in _PROSE_SAFE]
]


def detect_employment(title: str, description: str = "", raw: str = "") -> str:
    """Which employment kind this posting is, or "" when it never says.

    Title first (employers put it there when it matters), then the source's own
    field, then the opening of the description.
    """
    title_text = f" {(title or '').lower()} "
    raw_text = f" {_normalise_raw(raw)} "
    body = f" {(description or '')[:1200].lower()} "

    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(title_text):
            return kind
    # A board's own label is a trusted field, so bare "placement" leading it means
    # the kind - RateMyPlacement's "Placement (10 Months+)", Gradcracker's
    # "Placement/Internship". In a title it would not: "Placement Officer" is a job.
    if raw_text.strip().startswith("placement"):
        return "placement"
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(raw_text):
            return kind
    # The description is a last resort and only for phrases that cannot mean
    # anything else.
    for kind, pattern in _KIND_PROSE_PATTERNS:
        if pattern.search(body):
            return kind
    return ""


# -------------------------------------------------------------------- job family
# Checked in order; the first family with a title hit wins. Keep the distinctive
# words - "engineer" alone is too broad to separate software from mechanical.
JOB_FAMILIES: list[tuple[str, str, list[str]]] = [
    ("hospitality", "Hospitality & food", [
        "barista", "waiter", "waitress", "waiting staff", "front of house",
        "bartender", "bar staff", "chef", "kitchen porter", "kitchen assistant",
        "cook", "catering", "restaurant", "cafe", "coffee shop", "barback",
        "food and beverage", "hospitality", "crew member", "team member",
        "commis", "sous chef", "host ", "hostess", "pub", "bar team",
    ]),
    ("retail", "Retail", [
        "retail", "sales assistant", "store assistant", "shop assistant",
        "cashier", "checkout", "till operator", "store colleague",
        "customer assistant", "sales advisor", "store manager", "shop floor",
        "merchandiser", "stock assistant", "visual merchandis", "store team",
        "supermarket", "shelf", "personal shopper",
    ]),
    ("care", "Care & support work", [
        "care assistant", "carer", "support worker", "healthcare assistant",
        "care worker", "domiciliary", "residential care", "nursing assistant",
    ]),
    ("healthcare", "Healthcare & clinical", [
        "nurse", "nursing", "doctor", "physician", "clinical", "pharmacist",
        "radiograph", "physiotherap", "dental", "paramedic", "midwife",
        "medical officer", "healthcare scientist",
    ]),
    ("education", "Education & tutoring", [
        "teacher", "teaching assistant", "tutor", "lecturer", "academic",
        "school", "nursery", "early years", "education", "invigilator",
    ]),
    ("driving", "Driving & delivery", [
        # "delivery" on its own is ambiguous - software teams ship "delivery"
        # too - so it must carry a transport noun. Real driving jobs still match
        # on "driver", "courier" or "hgv".
        "driver", "courier", "hgv", "lgv", "van driver", "delivery driver",
        "delivery rider", "parcel delivery", "food delivery", "delivery cyclist",
        "rider", "chauffeur", "forklift",
    ]),
    ("warehouse", "Warehouse & logistics", [
        "warehouse", "picker", "packer", "logistics", "supply chain",
        "fulfilment", "fulfillment", "stock controller", "goods in",
        "distribution centre", "operative",
    ]),
    ("security", "Security", [
        "security officer", "security guard", "door supervisor", "cctv",
    ]),
    ("cleaning", "Cleaning & facilities", [
        "cleaner", "cleaning", "housekeeping", "janitor", "caretaker",
        "facilities assistant", "custodian",
    ]),
    ("customer_service", "Customer service", [
        "customer service", "customer support", "call centre", "call center",
        "contact centre", "helpdesk", "help desk", "customer advisor",
        "customer experience", "client services", "receptionist",
    ]),
    ("software", "Software & IT", [
        "software engineer", "developer", "programmer", "full stack", "frontend",
        "front end", "backend", "back end", "devops", "site reliability",
        "platform engineer", "mobile engineer", "ios engineer", "android engineer",
        "qa engineer", "test engineer", "security engineer", "cloud engineer",
        "it support", "systems administrator", "network engineer", "web developer",
    ]),
    ("data", "Data & analytics", [
        "data analyst", "data scientist", "data engineer", "machine learning",
        "analytics", "business intelligence", "quantitative", "statistician",
        "research scientist", "ai engineer",
    ]),
    ("engineering", "Engineering (non-software)", [
        "mechanical engineer", "electrical engineer", "civil engineer",
        "design engineer", "manufacturing engineer", "process engineer",
        "chemical engineer", "aerospace", "structural engineer", "cad",
        "maintenance engineer", "project engineer", "biomedical engineer",
        "materials engineer", "quality engineer", "hardware engineer",
        "systems engineer", "production engineer", "graduate engineer",
        "engineering", "technician",
        # Bare "engineer" last: software and data are checked before this family,
        # so anything still unmatched here is engineering in the broad sense.
        "engineer",
    ]),
    ("science", "Science & lab", [
        "laboratory", "lab technician", "scientist", "chemist", "biologist",
        "research associate", "microbiolog", "formulation",
    ]),
    ("construction", "Construction & trades", [
        "construction", "site manager", "quantity surveyor", "electrician",
        "plumber", "carpenter", "labourer", "bricklayer", "scaffolder",
        "site engineer", "groundworker",
    ]),
    ("finance", "Finance & accounting", [
        "accountant", "accounting", "finance", "audit", "tax ", "actuar",
        "investment", "trader", "trading", "risk analyst", "treasury",
        "financial analyst", "credit analyst", "banking", "payroll",
    ]),
    ("legal", "Legal", [
        "solicitor", "paralegal", "legal counsel", "barrister", "compliance officer",
        "legal assistant", "contracts manager",
    ]),
    ("marketing", "Marketing & communications", [
        "marketing", "social media", "content", "copywriter", "brand",
        "communications", "pr ", "seo", "growth marketing", "campaign",
    ]),
    ("sales", "Sales & business development", [
        "sales executive", "business development", "account executive",
        "account manager", "sales manager", "partnerships", "sdr ",
        "telesales", "sales representative",
    ]),
    ("hr", "HR & recruitment", [
        "human resources", "recruit", "talent acquisition", "people team",
        "people operations", "hr ",
    ]),
    ("admin", "Admin & office", [
        "administrator", "admin assistant", "office manager", "secretary",
        "personal assistant", "executive assistant", "data entry", "clerk",
        "office assistant", "coordinator",
    ]),
    ("creative", "Design & creative", [
        "graphic design", "ux ", "ui design", "product design", "illustrator",
        "photographer", "video editor", "animator", "creative",
    ]),
    ("operations", "Operations & management", [
        "operations", "project manager", "programme manager", "product manager",
        "general manager", "business analyst", "consultant", "strategy",
    ]),
]

# Phrases that identify a job in a TITLE but say nothing in prose - almost every
# description contains "team member", "support" or "content" somewhere. Using them
# in the description fallback tagged a quant internship as hospitality.
TITLE_ONLY_SIGNALS = {
    "team member", "crew member", "host ", "pub", "cook", "shelf", "operative",
    "content", "brand", "coordinator", "consultant", "strategy", "academic",
    "school", "education", "clinical", "creative", "engineering", "engineer",
    "technician", "logistics", "supply chain", "delivery", "rider", "partnerships",
    "communications", "client services", "customer experience", "people team",
}

_FAMILY_LABELS = {key: label for key, label, _ in JOB_FAMILIES}
FAMILY_ORDER = [key for key, _, _ in JOB_FAMILIES]


def family_label(family: str) -> str:
    return _FAMILY_LABELS.get(family, "Other")


_FAMILY_PATTERNS = [(key, _compile(sigs)) for key, _, sigs in JOB_FAMILIES]
# The description fallback drops phrases that are only meaningful in a title.
_FAMILY_CONTEXT_PATTERNS = [
    (key, _compile([s for s in sigs if s not in TITLE_ONLY_SIGNALS]))
    for key, _, sigs in JOB_FAMILIES
    if [s for s in sigs if s not in TITLE_ONLY_SIGNALS]
]


def detect_family(title: str, department: str = "", description: str = "") -> str:
    """Which family of work this is, or "" when nothing matches.

    The title carries the signal; the department field is the only fallback.
    """
    title_text = f" {(title or '').lower()} "
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(title_text):
            return family

    # Only the department is consulted as a fallback, never the description.
    # Description prose is actively misleading: a payments company writes "retail",
    # a software team writes "delivery", a data role writes "cleaning". Each of
    # those produced a confidently wrong family. Leaving it unset is more useful
    # than a wrong label, and the UI can show "not stated".
    context = f" {(department or '').lower()} "
    if context.strip():
        for family, pattern in _FAMILY_CONTEXT_PATTERNS:
            if pattern.search(context):
                return family
    return ""


def classify(title: str, description: str = "", raw_employment: str = "",
             department: str = "") -> tuple[str, str]:
    """Both labels at once: (employment_kind, job_family). Either may be ""."""
    return (detect_employment(title, description, raw_employment),
            detect_family(title, department, description))


# Loose words people type, and older configs stored, mapped onto the real keys.
_KIND_ALIASES = {
    "graduate": "graduate_scheme", "grad": "graduate_scheme",
    "grad scheme": "graduate_scheme", "temporary": "seasonal", "temp": "seasonal",
    "fixed term": "seasonal", "parttime": "part_time", "part time": "part_time",
    "fulltime": "full_time", "full time": "full_time", "permanent": "full_time",
    "intern": "internship", "placement year": "placement",
    "year in industry": "placement", "freelance": "contract",
    "zero hours": "zero_hours", "casual": "zero_hours", "voluntary": "volunteer",
}
_FAMILY_ALIASES = {
    "food": "hospitality", "catering": "hospitality", "shop": "retail",
    "store": "retail", "it": "software", "tech": "software",
    "developer": "software", "analytics": "data", "logistics": "warehouse",
    "office": "admin", "support": "customer_service", "nursing": "healthcare",
    "trades": "construction", "design": "creative", "management": "operations",
}


def normalise_kind(value: str) -> str:
    key = re.sub(r"[\s_\-/]+", " ", (value or "").strip().lower())
    if key.replace(" ", "_") in _KIND_LABELS:
        return key.replace(" ", "_")
    return _KIND_ALIASES.get(key, key.replace(" ", "_"))


def normalise_family(value: str) -> str:
    key = re.sub(r"[\s_\-/]+", " ", (value or "").strip().lower())
    if key.replace(" ", "_") in _FAMILY_LABELS:
        return key.replace(" ", "_")
    return _FAMILY_ALIASES.get(key, key.replace(" ", "_"))


def choices() -> dict:
    """For the UI pickers."""
    return {
        "employment": [{"key": k, "label": lbl} for k, lbl, _ in EMPLOYMENT_KINDS],
        "families": [{"key": k, "label": lbl} for k, lbl, _ in JOB_FAMILIES],
    }
