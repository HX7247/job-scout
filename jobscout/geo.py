"""Country registry - everything that used to be hardcoded to the UK.

Drives four things that must change per country:
  * remote eligibility - "Remote - United States" is not a London job, and
    "Remote - Europe" is not a Sydney job. Generalised here rather than as a
    blocklist of "foreign" places.
  * salary parsing - currency symbol and code per market.
  * aggregator routing - Adzuna's country code; boards that only serve one market.
  * tracker formatting - date format and the job-board names in the Source dropdown.

Matching never uses bare ISO codes as search terms ("us", "in", "it" are all common
English words). Only full names, unambiguous aliases and major cities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Regions, for "Remote - EMEA" style scoping.
REGION_TERMS = {
    "europe":        ["europe", "european", "emea", "eu-based", "eea"],
    "north_america": ["north america", "north american", "namer", "amer", "nam"],
    "latin_america": ["latin america", "latam", "south america", "central america"],
    "apac":          ["apac", "asia pacific", "asia-pacific", "asia", "sea region"],
    "mea":           ["mea", "middle east", "gcc", "africa"],
}

GLOBAL_TERMS = ["worldwide", "anywhere", "global", "globally", "any location",
                "fully remote", "location independent", "work from anywhere",
                "distributed", "no location requirement"]


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    region: str
    currency: str
    symbol: str
    aliases: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    adzuna: str = ""              # Adzuna country code, "" if unsupported
    date_format: str = "dd/mm/yyyy"
    boards: tuple[str, ...] = ()  # market-specific job boards, for the tracker dropdown

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.name.lower(),) + self.aliases + self.cities


# ---------------------------------------------------------------- the registry
_C = Country
COUNTRIES: dict[str, Country] = {c.code: c for c in [
    _C("GB", "United Kingdom", "europe", "GBP", "£",
       ("uk", "u.k.", "great britain", "britain", "england", "scotland", "wales",
        "northern ireland"),
       # A long list on purpose. Retail, hospitality and care work is spread across
       # every town, and an employer's own board lists a bare town name with no
       # country - so "Carlisle" or "Bootle" must resolve or those jobs vanish.
       ("london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
        "bristol", "sheffield", "liverpool", "cardiff", "belfast", "oxford",
        "reading", "coventry", "nottingham", "newcastle", "leicester", "bradford",
        "wolverhampton", "plymouth", "southampton", "swansea", "salford", "aberdeen",
        "westminster", "portsmouth", "york", "peterborough", "dundee", "lancaster",
        "brighton", "hull", "stoke-on-trent", "derby", "sunderland", "exeter",
        "gloucester", "bath", "norwich", "luton", "milton keynes", "northampton",
        "preston", "swindon", "middlesbrough", "bournemouth", "ipswich", "blackpool",
        "west bromwich", "telford", "slough", "warrington", "huddersfield", "poole",
        "oldham", "basildon", "chelmsford", "colchester", "crawley", "gillingham",
        "solihull", "rotherham", "stockport", "wigan", "st helens", "bolton",
        "bury", "rochdale", "blackburn", "burnley", "carlisle", "chester",
        "doncaster", "barnsley", "wakefield", "halifax", "harrogate", "scunthorpe",
        "grimsby", "lincoln", "mansfield", "chesterfield", "shrewsbury", "worcester",
        "hereford", "cheltenham", "taunton", "yeovil", "salisbury", "winchester",
        "guildford", "woking", "watford", "st albans", "hemel hempstead", "stevenage",
        "cambridge", "bedford", "high wycombe", "aylesbury", "basingstoke",
        "eastbourne", "hastings", "maidstone", "canterbury", "dover", "folkestone",
        "southend", "bootle", "birkenhead", "wallasey", "southport", "crewe",
        "macclesfield", "stafford", "walsall", "dudley", "sutton coldfield",
        "nuneaton", "rugby", "kettering", "corby", "loughborough", "grantham",
        "kings lynn", "great yarmouth", "lowestoft", "bury st edmunds",
        "clydebank", "paisley", "motherwell", "kilmarnock", "ayr",
        "falkirk", "stirling", "inverness", "livingston", "cumbernauld",
        "newport", "wrexham", "barry", "neath", "bridgend", "llanelli", "brynmawr",
        "merthyr tydfil", "pontypridd", "colwyn bay", "bangor", "workington",
        "whitehaven", "kendal", "barrow-in-furness", "darlington", "hartlepool",
        "stockton-on-tees", "redcar", "durham", "gateshead", "south shields",
        "ashington", "morpeth", "berwick", "londonderry", "lisburn", "newry",
        "ballymena", "coleraine", "craigavon", "charlton", "croydon", "bromley",
        "ealing", "enfield", "harrow", "hounslow", "kingston upon thames",
        "richmond upon thames", "sutton", "barnet", "brent", "camden", "greenwich",
        "hackney", "haringey", "islington", "lambeth", "lewisham", "merton",
        "newham", "redbridge", "southwark", "tower hamlets", "waltham forest",
        "wandsworth", "romford", "ilford", "uxbridge", "wembley", "stratford"),
       adzuna="gb",
       boards=("Bright Network", "RateMyPlacement", "Gradcracker", "Milkround",
               "TargetJobs", "Prospects")),

    _C("US", "United States", "north_america", "USD", "$",
       # State names matter as much as city names: a bare "Durham" or "Lancaster"
       # collides with a UK town, and only the state says which country it is in.
       ("usa", "u.s.", "u.s.a", "us-remote", "us remote", "remote - us", "us only",
        "us-based", "united states of america",
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
        "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
        "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
        "new mexico", "north carolina", "north dakota", "ohio", "oklahoma",
        "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington state",
        "west virginia", "wisconsin", "wyoming"),
       ("san francisco", "new york", "seattle", "austin", "boston", "chicago",
        "los angeles", "denver", "atlanta", "san diego", "portland", "miami",
        "washington dc", "philadelphia", "dallas", "houston", "phoenix"),
       adzuna="us", date_format="mm/dd/yyyy",
       boards=("Handshake", "Indeed", "ZipRecruiter", "Glassdoor", "Idealist")),

    _C("CA", "Canada", "north_america", "CAD", "$",
       ("canadian", "ontario", "quebec", "british columbia", "alberta",
        "manitoba", "saskatchewan", "nova scotia", "new brunswick"),
       ("toronto", "vancouver", "montreal", "ottawa", "calgary", "edmonton",
        "waterloo", "quebec city", "winnipeg"),
       adzuna="ca", boards=("Indeed Canada", "Job Bank", "TalentEgg")),

    _C("IE", "Ireland", "europe", "EUR", "€", ("irish", "republic of ireland"),
       ("dublin", "cork", "galway", "limerick"),
       boards=("IrishJobs", "GradIreland", "Jobs.ie")),

    _C("AU", "Australia", "apac", "AUD", "$", ("australian", "aus"),
       ("sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra"),
       adzuna="au", boards=("SEEK", "GradConnection", "Indeed Australia")),

    _C("NZ", "New Zealand", "apac", "NZD", "$", ("kiwi",),
       ("auckland", "wellington", "christchurch"),
       adzuna="nz", boards=("SEEK NZ", "Trade Me Jobs")),

    _C("DE", "Germany", "europe", "EUR", "€", ("german", "deutschland"),
       ("berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "köln",
        "stuttgart", "düsseldorf", "leipzig"),
       adzuna="de", date_format="dd.mm.yyyy",
       boards=("StepStone", "Xing", "Indeed Deutschland", "Absolventa")),

    _C("FR", "France", "europe", "EUR", "€", ("french",),
       ("paris", "lyon", "marseille", "toulouse", "bordeaux", "lille", "nantes",
        "grenoble", "sophia antipolis"),
       adzuna="fr", boards=("APEC", "Welcome to the Jungle", "Indeed France", "JobTeaser")),

    _C("NL", "Netherlands", "europe", "EUR", "€", ("dutch", "holland"),
       ("amsterdam", "rotterdam", "utrecht", "eindhoven", "the hague", "den haag",
        "delft", "groningen"),
       adzuna="nl", boards=("Indeed NL", "Nationale Vacaturebank", "Magnet.me")),

    _C("ES", "Spain", "europe", "EUR", "€", ("spanish", "españa"),
       ("madrid", "barcelona", "valencia", "seville", "sevilla", "malaga", "bilbao"),
       adzuna="es", boards=("InfoJobs", "Tecnoempleo", "Indeed España")),

    _C("IT", "Italy", "europe", "EUR", "€", ("italian", "italia"),
       ("milan", "milano", "rome", "roma", "turin", "torino", "bologna", "naples"),
       adzuna="it", boards=("InfoJobs IT", "Indeed Italia", "Monster IT")),

    _C("PT", "Portugal", "europe", "EUR", "€", ("portuguese",),
       ("lisbon", "lisboa", "porto", "braga"),
       boards=("Net-Empregos", "ITJobs", "Indeed Portugal")),

    _C("BE", "Belgium", "europe", "EUR", "€", ("belgian",),
       ("brussels", "antwerp", "ghent", "leuven"),
       adzuna="be", boards=("VDAB", "StepStone BE", "Indeed Belgium")),

    _C("CH", "Switzerland", "europe", "CHF", "CHF", ("swiss",),
       ("zurich", "zürich", "geneva", "basel", "lausanne", "bern", "zug"),
       adzuna="ch", boards=("jobs.ch", "JobUp", "Indeed Schweiz")),

    _C("AT", "Austria", "europe", "EUR", "€", ("austrian", "österreich"),
       ("vienna", "wien", "graz", "linz", "salzburg"),
       adzuna="at", date_format="dd.mm.yyyy", boards=("karriere.at", "StepStone AT")),

    _C("SE", "Sweden", "europe", "SEK", "kr", ("swedish", "sverige"),
       ("stockholm", "gothenburg", "göteborg", "malmö", "uppsala", "lund"),
       date_format="yyyy-mm-dd", boards=("Arbetsförmedlingen", "Blocket Jobb", "Academic Work")),

    _C("NO", "Norway", "europe", "NOK", "kr", ("norwegian", "norge"),
       ("oslo", "bergen", "trondheim", "stavanger"),
       boards=("Finn.no", "NAV", "Indeed Norge")),

    _C("DK", "Denmark", "europe", "DKK", "kr", ("danish", "danmark"),
       ("copenhagen", "københavn", "aarhus", "odense", "aalborg"),
       date_format="dd-mm-yyyy", boards=("Jobindex", "WorkInDenmark", "Graduateland")),

    _C("FI", "Finland", "europe", "EUR", "€", ("finnish", "suomi"),
       ("helsinki", "espoo", "tampere", "oulu", "turku"),
       date_format="dd.mm.yyyy", boards=("Duunitori", "Oikotie", "TE-palvelut")),

    _C("PL", "Poland", "europe", "PLN", "zł", ("polish", "polska"),
       ("warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk",
        "poznan", "katowice", "lodz"),
       adzuna="pl", date_format="dd.mm.yyyy",
       boards=("Pracuj.pl", "NoFluffJobs", "JustJoin.it")),

    _C("CZ", "Czechia", "europe", "CZK", "Kč", ("czech", "czech republic"),
       ("prague", "praha", "brno", "ostrava"),
       date_format="dd.mm.yyyy", boards=("Jobs.cz", "StartupJobs.cz")),

    _C("RO", "Romania", "europe", "RON", "lei", ("romanian",),
       ("bucharest", "bucuresti", "cluj", "cluj-napoca", "timisoara", "iasi"),
       date_format="dd.mm.yyyy", boards=("eJobs", "BestJobs", "Hipo.ro")),

    _C("IN", "India", "apac", "INR", "₹", ("indian", "bharat"),
       ("bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "hyderabad",
        "pune", "chennai", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad"),
       adzuna="in", boards=("Naukri", "Internshala", "LinkedIn India", "Instahyre")),

    _C("SG", "Singapore", "apac", "SGD", "$", ("singaporean",),
       ("singapore",), adzuna="sg", boards=("MyCareersFuture", "JobStreet SG", "JobsDB")),

    _C("HK", "Hong Kong", "apac", "HKD", "$", ("hong kong sar",),
       ("hong kong", "kowloon"), boards=("JobsDB HK", "CTgoodjobs")),

    _C("JP", "Japan", "apac", "JPY", "¥", ("japanese", "nippon"),
       ("tokyo", "osaka", "kyoto", "yokohama", "nagoya", "fukuoka"),
       date_format="yyyy/mm/dd", boards=("Rikunabi", "Mynavi", "Doda", "GaijinPot")),

    _C("KR", "South Korea", "apac", "KRW", "₩", ("korea", "korean", "republic of korea"),
       ("seoul", "busan", "incheon", "daejeon"),
       date_format="yyyy-mm-dd", boards=("JobKorea", "Saramin", "Wanted")),

    _C("CN", "China", "apac", "CNY", "¥", ("chinese", "prc", "mainland china"),
       ("beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu", "suzhou"),
       date_format="yyyy-mm-dd", boards=("51job", "Zhaopin", "BOSS Zhipin", "Liepin")),

    _C("MY", "Malaysia", "apac", "MYR", "RM", ("malaysian",),
       ("kuala lumpur", "penang", "johor bahru", "cyberjaya"),
       boards=("JobStreet", "Hiredly", "Maukerja")),

    _C("PH", "Philippines", "apac", "PHP", "₱", ("filipino", "philippine"),
       ("manila", "makati", "cebu", "taguig", "quezon city"),
       boards=("JobStreet PH", "Kalibrr", "Indeed PH")),

    _C("ID", "Indonesia", "apac", "IDR", "Rp", ("indonesian",),
       ("jakarta", "bandung", "surabaya", "yogyakarta"),
       boards=("Jobstreet ID", "Glints", "Kalibrr")),

    _C("AE", "United Arab Emirates", "mea", "AED", "AED", ("uae", "emirates"),
       ("dubai", "abu dhabi", "sharjah"),
       boards=("Bayt", "GulfTalent", "Naukrigulf")),

    _C("IL", "Israel", "mea", "ILS", "₪", ("israeli",),
       ("tel aviv", "jerusalem", "haifa", "herzliya"),
       boards=("AllJobs", "Drushim", "JobMaster")),

    _C("TR", "Turkey", "mea", "TRY", "₺", ("turkish", "türkiye"),
       ("istanbul", "ankara", "izmir"),
       date_format="dd.mm.yyyy", boards=("Kariyer.net", "Yenibiris")),

    _C("ZA", "South Africa", "mea", "ZAR", "R", ("south african",),
       ("johannesburg", "cape town", "durban", "pretoria"),
       adzuna="za", date_format="yyyy/mm/dd",
       boards=("PNet", "CareerJunction", "Careers24")),

    _C("NG", "Nigeria", "mea", "NGN", "₦", ("nigerian",),
       ("lagos", "abuja", "port harcourt"),
       boards=("Jobberman", "MyJobMag", "HotNigerianJobs")),

    _C("KE", "Kenya", "mea", "KES", "KSh", ("kenyan",),
       ("nairobi", "mombasa"), boards=("BrighterMonday", "Fuzu", "MyJobMag Kenya")),

    _C("BR", "Brazil", "latin_america", "BRL", "R$", ("brazilian", "brasil"),
       ("sao paulo", "são paulo", "rio de janeiro", "belo horizonte", "curitiba",
        "porto alegre", "brasilia"),
       adzuna="br", boards=("Catho", "InfoJobs BR", "Vagas.com", "Gupy")),

    _C("MX", "Mexico", "latin_america", "MXN", "$", ("mexican", "méxico"),
       ("mexico city", "ciudad de mexico", "guadalajara", "monterrey", "queretaro"),
       adzuna="mx", boards=("OCC Mundial", "Computrabajo", "Indeed México")),

    _C("AR", "Argentina", "latin_america", "ARS", "$", ("argentinian", "argentine"),
       ("buenos aires", "cordoba", "rosario"),
       boards=("Bumeran", "ZonaJobs", "Computrabajo AR")),

    _C("CL", "Chile", "latin_america", "CLP", "$", ("chilean",),
       ("santiago", "valparaiso"), boards=("Laborum", "Trabajando.com")),

    _C("CO", "Colombia", "latin_america", "COP", "$", ("colombian",),
       ("bogota", "bogotá", "medellin", "medellín", "cali"),
       boards=("Computrabajo CO", "elempleo")),
]}

REGIONS = sorted({c.region for c in COUNTRIES.values()})

# Boards every market has, appended to the country-specific ones.
UNIVERSAL_BOARDS = ("Company website", "LinkedIn", "Indeed", "Glassdoor",
                    "University careers service", "Careers fair", "Referral",
                    "Job Scout", "Other")


# ------------------------------------------------------------------ resolution
def get(code_or_name: str | None) -> Country | None:
    """Resolve 'GB', 'uk', 'United Kingdom' or 'london' to a Country."""
    if not code_or_name:
        return None
    key = str(code_or_name).strip().lower()
    if key.upper() in COUNTRIES:
        return COUNTRIES[key.upper()]
    for country in COUNTRIES.values():
        if key == country.name.lower() or key in country.aliases:
            return country
    for country in COUNTRIES.values():
        if key in country.cities:
            return country
    return None


def resolve_from_locations(locations: list[str]) -> Country | None:
    """Infer the country from the configured location list, best match first."""
    for location in locations or []:
        country = get(location)
        if country:
            return country
    return None


# --------------------------------------------------------------- text matching
def _build_lookup() -> tuple[re.Pattern, dict[str, str]]:
    """One alternation over every country term, longest first so 'new york' wins."""
    mapping: dict[str, str] = {}
    for country in COUNTRIES.values():
        for term in country.terms:
            mapping.setdefault(term, country.code)
    ordered = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in ordered) + r")(?![a-z0-9])",
        re.I,
    )
    return pattern, mapping


_LOOKUP_RE, _TERM_TO_CODE = _build_lookup()


def countries_mentioned(text: str) -> set[str]:
    """ISO codes for every country named in the text."""
    if not text:
        return set()
    return {_TERM_TO_CODE[m.group(1).lower()]
            for m in _LOOKUP_RE.finditer(text.lower())
            if m.group(1).lower() in _TERM_TO_CODE}


def primary_country(text: str) -> str | None:
    """The country a location string is really about.

    Town names collide across countries - there is a Lancaster in England and one in
    Pennsylvania - so a bare town is not enough. The longest matching term wins,
    because a state or country name is longer and more specific than the town it
    qualifies: "Lancaster, Pennsylvania" resolves to US, "Lancaster" alone to GB.
    """
    if not text:
        return None
    best_code, best_len = None, 0
    for match in _LOOKUP_RE.finditer(text.lower()):
        term = match.group(1).lower()
        code = _TERM_TO_CODE.get(term)
        if code and len(term) > best_len:
            best_code, best_len = code, len(term)
    return best_code


def mentions_any(text: str, terms: list[str]) -> bool:
    low = (text or "").lower()
    return any(t in low for t in terms)


def remote_eligible(text: str, home: Country | None) -> bool:
    """Could someone based in `home` actually take this remote role?

    Replaces the old UK-specific blocklist. The rule is symmetric for any country:
      - names your country            -> yes
      - says worldwide/anywhere       -> yes
      - names your region (EMEA, ...) -> yes
      - names other countries only    -> no
      - names nowhere                 -> yes, the description decides
    """
    if home is None:
        return True
    low = (text or "").lower()

    mentioned = countries_mentioned(low)
    if home.code in mentioned:
        return True
    if mentions_any(low, GLOBAL_TERMS):
        return True
    if mentions_any(low, REGION_TERMS.get(home.region, [])):
        return True
    # Scoped to somewhere else entirely.
    if mentioned:
        return False
    # A region we are not in, with no country named ("Remote - LATAM").
    for region, terms in REGION_TERMS.items():
        if region != home.region and mentions_any(low, terms):
            return False
    return True


# ---------------------------------------------------------------- salary parsing
# Non-alphabetic symbols are safe to match anywhere; alphabetic codes need word
# boundaries ("R" for rand would otherwise fire on every capital R in the text).
_SYMBOLS = {
    "R$": "BRL", "RM": "MYR", "Rp": "IDR", "KSh": "KES", "zł": "PLN", "Kč": "CZK",
    "£": "GBP", "€": "EUR", "$": "USD", "¥": "JPY", "₹": "INR", "₩": "KRW",
    "₱": "PHP", "₦": "NGN", "₪": "ILS", "₺": "TRY", "kr": "SEK", "lei": "RON",
}
_CODES = sorted({c.currency for c in COUNTRIES.values()})
_CODE_RE = re.compile(r"(?<![a-z])(" + "|".join(_CODES) + r")(?![a-z])", re.I)

# Symbols shared by several markets - resolve against the user's own country first.
_AMBIGUOUS = {"$": ("USD", "CAD", "AUD", "SGD", "NZD", "HKD", "MXN", "ARS", "CLP", "COP"),
              "¥": ("JPY", "CNY"), "kr": ("SEK", "NOK", "DKK")}

_NUMBER_RE = re.compile(r"\d[\d,.  ]{0,14}\d|\d{4,9}")

# Plausible annual pay, per currency, so a phone number is not read as a salary.
_BOUNDS = {
    "JPY": (1_500_000, 60_000_000), "KRW": (15_000_000, 500_000_000),
    "INR": (100_000, 20_000_000),   "IDR": (30_000_000, 3_000_000_000),
    "PHP": (150_000, 10_000_000),   "CLP": (4_000_000, 200_000_000),
    "COP": (10_000_000, 900_000_000), "NGN": (500_000, 100_000_000),
    "KES": (300_000, 30_000_000),   "ZAR": (100_000, 5_000_000),
    "TRY": (100_000, 10_000_000),   "CZK": (200_000, 5_000_000),
    "PLN": (30_000, 800_000),       "SEK": (200_000, 3_000_000),
    "NOK": (250_000, 3_000_000),    "DKK": (200_000, 2_500_000),
    "RON": (30_000, 800_000),       "BRL": (20_000, 1_000_000),
    "MXN": (80_000, 5_000_000),     "ARS": (1_000_000, 100_000_000),
    "CNY": (50_000, 3_000_000),     "MYR": (20_000, 1_000_000),
    "IDR ": (30_000_000, 3_000_000_000),
}
_DEFAULT_BOUNDS = (8_000, 500_000)


def _clean_number(raw: str) -> float | None:
    """'32,000' / '45.000' / '12,00,000' -> float. Separators vary by locale."""
    raw = raw.strip().replace(" ", "").replace(" ", "")
    # A single trailing group of exactly 3 digits after . or , is a thousands
    # separator, not a decimal point.
    if re.fullmatch(r"[\d.,]+", raw):
        if raw.count(".") == 1 and len(raw.rsplit(".", 1)[1]) == 3:
            raw = raw.replace(".", "")
        if raw.count(",") == 1 and len(raw.rsplit(",", 1)[1]) != 3:
            raw = raw.replace(",", ".")     # decimal comma, e.g. "50,5"
        raw = raw.replace(",", "")
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _currency_near(context: str, home: Country | None) -> str:
    """Find the currency quoted around a number, preferring the home market."""
    for symbol, currency in sorted(_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
        if symbol in context:
            options = _AMBIGUOUS.get(symbol)
            if options and home and home.currency in options:
                return home.currency
            return currency
    match = _CODE_RE.search(context)
    return match.group(1).upper() if match else ""


def parse_salary(text: str, home: Country | None = None
                 ) -> tuple[float | None, float | None, str]:
    """Pull a pay range out of free text, in whatever currency it is quoted.

    A number with no currency anywhere near it is ignored - guessing turns
    '50,000 employees' into a salary.
    """
    if not text:
        return None, None, ""
    window = text[:4000]
    found: dict[str, list[float]] = {}

    for match in _NUMBER_RE.finditer(window):
        context = window[max(0, match.start() - 12):match.end() + 14]
        currency = _currency_near(context, home)
        if not currency:
            continue
        value = _clean_number(match.group(0))
        if value is None:
            continue
        if re.match(r"\s*k(?![a-z])", window[match.end():match.end() + 3], re.I):
            value *= 1000                 # "45k" -> 45,000
        low, high = _BOUNDS.get(currency, _DEFAULT_BOUNDS)
        if low <= value <= high:
            found.setdefault(currency, []).append(value)

    if not found:
        return None, None, ""
    currency = (home.currency if home and home.currency in found
                else max(found, key=lambda c: len(found[c])))
    values = found[currency]
    return min(values), max(values), currency


def format_salary(low: float | None, high: float | None, currency: str) -> str:
    if not (low or high):
        return ""
    symbol = next((c.symbol for c in COUNTRIES.values() if c.currency == currency), "")
    prefix = symbol if symbol and not symbol.isalpha() else (
        f"{symbol or currency} " if (symbol or currency) else "")
    if low and high and low != high:
        return f"{prefix}{low:,.0f} - {prefix}{high:,.0f}"
    return f"{prefix}{(low or high):,.0f}"


def tracker_sources(home: Country | None) -> list[str]:
    """Job-board names for the tracker's Source dropdown, tuned to the market."""
    boards = list(home.boards) if home else []
    for board in UNIVERSAL_BOARDS:
        if board not in boards:
            boards.append(board)
    return boards


def choices() -> list[dict]:
    """For the UI country picker."""
    return sorted(
        ({"code": c.code, "name": c.name, "currency": c.currency,
          "adzuna": bool(c.adzuna), "region": c.region} for c in COUNTRIES.values()),
        key=lambda c: c["name"],
    )
