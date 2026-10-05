"""Edge-case audit across every module. Finds the failures real data will cause.

Run:  python tools/audit.py
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.CRITICAL)

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, fn):
    """Run one assertion. A raised exception is a finding, not a crash."""
    global CHECKS
    CHECKS += 1
    try:
        result = fn()
    except Exception as exc:
        FAILURES.append(f"{label}: raised {type(exc).__name__}: {exc}")
        return
    if result is not True:
        FAILURES.append(f"{label}: {result}")


def _raises(expected, fn, *args) -> object:
    """True when fn raises what it should. Used for the account validation checks."""
    try:
        fn(*args)
    except expected:
        return True
    except Exception as exc:
        return f"raised {type(exc).__name__} instead of {expected.__name__}: {exc}"
    return f"did not raise {expected.__name__}"


def _dead_links(urls: list[str], timeout: float = 12.0) -> object:
    """True when every URL returns 200 right now, else which ones did not.

    A real network call, on the same principle sponsorship.register_for() already
    relies on this suite touching the network (it downloads the live government sponsor
    register on first run) - a hardcoded external link is only as good as its last
    verification, and interview.py shipped several 404s this session precisely because
    they were guessed rather than checked. A rotted link is a genuine finding, not
    noise: the alternative is a user clicking something broken with no way to know.
    """
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobScoutAudit/0.1)"}
    dead = []
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                dead.append(f"{url} -> {resp.status_code}")
        except requests.RequestException as exc:
            dead.append(f"{url} -> {type(exc).__name__}")
    return True if not dead else "dead: " + "; ".join(dead)


def _real_state() -> tuple:
    """A fingerprint of everything the audit must never touch: the real job rows'
    scores and flags, the real per-account folders, and the real config.yaml."""
    import hashlib
    import sqlite3
    digest = hashlib.sha1()
    db = ROOT / "data" / "jobscout.db"
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            wanted = [c for c in ("id", "score", "filtered", "status", "starred",
                                  "viewed_at") if c in columns]
            for row in conn.execute(f"SELECT {', '.join(wanted)} FROM jobs ORDER BY id"):
                digest.update(repr(tuple(row)).encode())
        finally:
            conn.close()
    accounts_dir = ROOT / "data" / "accounts"
    folders = sorted(p.name for p in accounts_dir.iterdir()) if accounts_dir.exists() else []
    config = ROOT / "config.yaml"
    config_digest = hashlib.sha1(config.read_bytes() if config.exists() else b"").hexdigest()
    return digest.hexdigest(), folders, config_digest


def main() -> None:
    _real_before = _real_state()
    from jobscout import classify, geo, sponsorship
    from jobscout import humanise
    from jobscout.apply import build_pack
    from jobscout.cv import CVProfile
    from jobscout.assistant import parse_rules
    from jobscout.config import Config
    from jobscout.cv import extract_skills, _rank_degrees, display_degree
    from jobscout.models import Job, strip_html, parse_date
    from jobscout.scoring import (hard_filter, score_job, detect_work_mode,
                                  _location_eligible, rank)
    from jobscout.store import Store
    from jobscout import tracker

    cfg = Config.load()

    # ---------------------------------------------------------------- models
    check("strip_html(None)", lambda: strip_html(None) == "" or "not empty")
    check("strip_html double-escaped",
          lambda: "<p>" not in strip_html("&lt;p&gt;Hello&lt;/p&gt;") or "tag leaked")
    check("strip_html nested entities",
          lambda: strip_html("&amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;").find("<") == -1
                  or "tag leaked")
    check("parse_date garbage", lambda: parse_date("not a date") is None or "parsed junk")
    check("parse_date epoch ms",
          lambda: (parse_date(1700000000000) or "").startswith("2023") or "bad epoch")

    blank = Job(source="s", source_kind="k", company="", title="", url="")
    check("Job with empty strings has an id", lambda: bool(blank.id) or "no id")
    check("Job None fields coerced",
          lambda: Job(source="s", source_kind="k", company="C", title="T", url="u",
                      department=None, location=None).department == "" or "None leaked")
    check("salary_display no salary", lambda: blank.salary_display == "" or "phantom pay")

    # ------------------------------------------------------------------- geo
    check("geo.get(None)", lambda: geo.get(None) is None or "resolved nothing")
    check("geo.get unknown", lambda: geo.get("Atlantis") is None or "invented a country")
    check("remote_eligible(None home)",
          lambda: geo.remote_eligible("Remote - US", None) is True or "should allow")
    check("parse_salary empty", lambda: geo.parse_salary("") == (None, None, "") or "bad")
    check("parse_salary no currency",
          lambda: geo.parse_salary("50,000 employees", geo.get("GB"))[0] is None
                  or "read headcount as pay")
    check("format_salary zeros", lambda: geo.format_salary(None, None, "") == "" or "bad")
    check("every country has a currency",
          lambda: all(c.currency and c.symbol for c in geo.COUNTRIES.values())
                  or "missing currency")
    check("every country region is known",
          lambda: all(c.region in geo.REGION_TERMS for c in geo.COUNTRIES.values())
                  or "unknown region")

    # -------------------------------------------------------------------- cv
    check("extract_skills empty", lambda: extract_skills("") == [] or "invented skills")
    check("degree ranking prefers MEng",
          lambda: _rank_degrees(["Master", "MEng"])[0] == "MENG" or "generic won")
    check("display_degree unknown",
          lambda: display_degree("XYZ") == "Xyz" or "bad fallback")

    # --------------------------------------------------------------- scoring
    empty_cfg = Config()
    check("hard_filter with default config",
          lambda: hard_filter(blank, empty_cfg) is None
                  or f"dropped a blank job: {hard_filter(blank, empty_cfg)}")
    check("score_job with no CV",
          lambda: 0 <= score_job(Job(source="s", source_kind="ats_direct", company="C",
                                     title="Graduate Engineer", url="u",
                                     location="London"), cfg, None).score <= 100
                  or "score out of range")
    check("detect_work_mode silent posting",
          lambda: detect_work_mode(Job(source="s", source_kind="k", company="C",
                                       title="Engineer", url="u")) == ""
                  or "guessed from nothing")
    check("rank([]) is empty",
          lambda: rank([], cfg, None) == ([], {}) or "invented rows")
    check("location_eligible no locations",
          lambda: _location_eligible(blank, empty_cfg.search, None) is True
                  or "dropped with no criteria")

    # --------------------------------------------------------------- store
    store = Store()
    check("store.get(missing)", lambda: store.get("nope") is None or "invented a row")
    check("set_status rejects nonsense",
          lambda: store.set_status("nope", "banana") is False or "accepted bad status")
    check("query limit 0 safe", lambda: store.query(limit=1) is not None or "failed")
    check("stats has all keys",
          lambda: {"total", "hidden", "sponsored", "by_status"} <= set(store.stats())
                  or "missing keys")

    # ---------------------------------------------------------- sponsorship
    check("sponsorship normalise empty",
          lambda: sponsorship.normalise("") == "" or "invented a name")
    check("sponsorship.check unknown country",
          lambda: sponsorship.check("Acme", "XX") is None or "invented a register")
    check("sponsorship.check blank company",
          lambda: sponsorship.check("", "GB") is None or "matched nothing to something")
    check("register_for(None)",
          lambda: sponsorship.register_for(None) is None or "invented a register")

    # --------------------------------------------------------------- tracker
    check("export_new with no jobs",
          lambda: tracker.export_new([], ROOT / "out" / "_audit_empty.xlsx")["added"] == 0
                  or "wrote phantom rows")
    check("job_to_row length",
          lambda: len(tracker.job_to_row({"company": "C", "title": "T"})) == 19
                  or "column count drift")

    # ------------------------------------------------------------- assistant
    check("assistant empty message",
          lambda: parse_rules("")[1] == [] or "acted on nothing")
    check("assistant gibberish",
          lambda: isinstance(parse_rules("asdfgh qwerty")[0], str) or "no reply")
    check("assistant does not mutate config",
          lambda: (parse_rules("only London"), Config.load().search.locations)[1]
                  == cfg.search.locations or "parser mutated saved config")

    # ------------------------------------------------------------------ apply
    job = Job(source="greenhouse", source_kind="ats_direct", company="Acme",
              title="Graduate Engineer", url="https://example.com/apply",
              location="London", description="We need python and cad.")
    check("build_pack with no CV",
          lambda: bool(build_pack(job, None, cfg).cover_letter) or "empty letter")
    check("build_pack no apply url",
          lambda: any("no apply url" in w.lower() for w in
                      build_pack(Job(source="s", source_kind="k", company="C",
                                     title="T", url=""), None, cfg).warnings)
                  or "missing warning")

    # Every check above passes profile=None, which is how a reference to a profile
    # field that no longer exists survived in _checklist: the line was never reached.
    # This one exercises the path a real user takes.
    _audit_profile = CVProfile(skills=["python", "solidworks"], degree="MEng",
                               field_of_study="Biomedical Engineering",
                               education_level="masters", source="linkedin_export")
    check("build_pack with a profile",
          lambda: bool(build_pack(job, _audit_profile, cfg).cover_letter)
          or "empty letter")
    check("build_pack names where the skills came from",
          lambda: "linkedin" in build_pack(job, _audit_profile, cfg)
                  .checklist[0]["note"].lower()
          or build_pack(job, _audit_profile, cfg).checklist[0]["note"])
    check("build_pack never asks for a name it does not have",
          lambda: "[Your name]" in build_pack(job, _audit_profile, cfg).cover_letter
          or "the letter signs itself off with something invented")
    check("build_pack letter is free of the obvious tells",
          lambda: humanise.score(
              build_pack(job, _audit_profile, cfg).cover_letter) >= 70
          or humanise.tells(build_pack(job, _audit_profile, cfg).cover_letter))


    # =====================================================================
    #  THE PAGE ITSELF
    # =====================================================================
    # A script included twice throws "Identifier already declared" and the whole file
    # stops running - silently, because the page still renders. That happened, and the
    # Profile tab simply did nothing until it was spotted.
    page = (ROOT / "templates" / "index.html").read_text("utf-8")
    import re as _re
    scripts = _re.findall(r'<script src="([^"]+)"', page)
    check("page: no script is included twice",
          lambda: len(scripts) == len(set(scripts))
          or f"duplicated: {[x for x in set(scripts) if scripts.count(x) > 1]}")
    check("page: every script referenced actually exists",
          lambda: all((ROOT / src.lstrip("/")).exists() for src in scripts)
          or [src for src in scripts if not (ROOT / src.lstrip("/")).exists()])
    check("page: every view has a tab and every tab has a view",
          lambda: ({m for m in _re.findall(r'class="tab[^"]*" *data-view="(\w+)"', page)}
                   == {m for m in _re.findall(r'id="view-(\w+)"', page)})
          or f"tabs {_re.findall(r'data-view=\"(\w+)\"', page)}")
    # A whole <section id="view-profile"> got pasted in twice by an earlier patch script
    # and nothing caught it: the check above compares SETS of ids, so two copies of the
    # same id look identical to one. Every id on the page has to be unique regardless -
    # a duplicate is invalid HTML, and for a data-view section specifically it means the
    # tab-switching code (which toggles visibility by matching against the class, not a
    # unique lookup) shows both copies stacked on top of each other at once.
    from collections import Counter as _Counter
    _id_counts = _Counter(_re.findall(r'\bid="([\w-]+)"', page))
    _dupes = {i: n for i, n in _id_counts.items() if n > 1}
    check("page: no element id appears more than once",
          lambda: not _dupes or f"duplicated: {_dupes}")

    # =====================================================================
    #  PRIVACY - the promises this app makes about the user's own data
    # =====================================================================
    import json as _json
    import zipfile as _zipfile
    from jobscout import interview, linkedin_import, privacy, rules as rules_mod
    from jobscout.accounts import AccountError, Accounts
    from jobscout.tracker import safe_url

    # A LinkedIn export full of personal data, exactly as LinkedIn ships one.
    fixture = ROOT / "out" / "_audit_export.zip"
    with _zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr(
            "Profile.csv",
            "First Name,Last Name,Address,Birth Date,Headline,Industry,Geo Location\n"
            "Ada,Lovelace,\"1 Test Street, London E1 6AN\",07/03/2001,"
            "\"MEng student\",Software,\"London, England, United Kingdom\"\n")
        archive.writestr(
            "Education.csv",
            "School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
            "Test University,Sep 2024,Jun 2028,,"
            "\"Master of Engineering - MEng, Software Engineering\",\n")
        archive.writestr("Skills.csv", "Name\nPython\nSQL\nDocker\n")
        archive.writestr(
            "Positions.csv",
            "Company Name,Title,Description,Location,Started On,Finished On\n"
            "Test Ltd,Intern,Wrote Python,London,Jun 2025,Sep 2025\n")
        archive.writestr("Email Addresses.csv",
                         "Email Address\nada@example.com\n")
        archive.writestr("PhoneNumbers.csv", "Number\n+44 7700 900999\n")
        archive.writestr("Connections.csv",
                         "First Name,Last Name,Email Address\nBob,Smith,bob@example.com\n")

    imported = linkedin_import.from_file(fixture)
    blob = _json.dumps(imported)

    check("import: no name kept",
          lambda: ("Lovelace" not in blob and "Ada" not in blob)
                  or f"name leaked into {blob[:120]}")
    check("import: no email kept",
          lambda: "ada@example.com" not in blob and "bob@example.com" not in blob
                  or "email leaked")
    check("import: no phone kept",
          lambda: "7700" not in blob or "phone number leaked")
    check("import: no address or postcode kept",
          lambda: ("Test Street" not in blob and "E1 6AN" not in blob)
                  or "address leaked")
    check("import: no date of birth kept",
          lambda: "07/03/2001" not in blob or "date of birth leaked")
    check("import: no connections kept",
          lambda: "Smith" not in blob or "connection list leaked")
    check("import: scrubber finds nothing identifying",
          lambda: privacy.contains_identifier(blob) == []
                  or privacy.contains_identifier(blob))
    check("import: every stored key is allowlisted",
          lambda: set(imported) - privacy.PROFILE_ALLOWLIST - {"career_stage"} == set()
                  or f"unexpected keys {set(imported) - privacy.PROFILE_ALLOWLIST}")

    check("import: reads the degree", lambda: imported["degree"] == "MEng"
          or imported["degree"])
    check("import: reads the subject",
          lambda: imported["field_of_study"] == "Software Engineering"
          or imported["field_of_study"])
    check("import: reads the skills",
          lambda: {"python", "sql", "docker"} <= set(imported["skills"])
          or imported["skills"])
    check("import: still studying means student",
          lambda: imported["career_stage"] == "student" or imported["career_stage"])

    # "Who's hiring in your field" - informational only, never a filter (see
    # linkedin_import.target_families' own docstring for why that distinction matters).
    check("target_families: reads a subject",
          lambda: set(linkedin_import.target_families(
              {"field_of_study": "Biomedical Engineering"})) >= {"engineering"}
          or "subject not recognised")
    check("target_families: every key is a real job_family",
          lambda: set(linkedin_import.target_families(imported))
                  <= {f["key"] for f in classify.choices()["families"]}
          or "invented a family key")
    check("target_families: no subject, no guess",
          lambda: linkedin_import.target_families({}) == [] or "invented a family")
    check("target_families: tolerates missing fields",
          lambda: linkedin_import.target_families(
              {"field_of_study": None, "headline": None}) == [] or "raised or guessed")
    check("store.top_employers: empty families asks nothing of sqlite",
          lambda: store.top_employers([]) == [] or "queried anyway")
    check("store.top_employers: an unknown family finds nobody",
          lambda: store.top_employers(["not_a_real_family"]) == [] or "invented a row")

    suggested = linkedin_import.suggestions(imported)
    check("suggestions: never propose a job-family filter from past jobs",
          lambda: suggested["job_families"] == []
          or "past employment must not become a hard filter - it hides the search")
    check("suggestions: propose titles from the subject",
          lambda: any("software" in t or "engineer" in t for t in suggested["titles"])
          or suggested["titles"])
    check("suggestions: explain themselves",
          lambda: len(suggested["why"]) >= 3 or suggested["why"])

    # ---- profile_review: the LinkedIn profile-strength review (this app's own user's
    # profile only - never fetched, always built from a file the user handed over)
    review_complete_profile = {
        "source": "linkedin_export", "region": "London, England",
        "industries": ["Software"], "education_level": "masters",
        "institution": "Test University", "recent_titles": ["Intern"],
        "years_experience": 0.5, "skills": ["python", "sql"], "languages": ["French"],
    }
    review_complete_tables = {
        "profile": [{"summary": "Wrote things."}],
        "positions": [{"description": "Wrote Python."}],
        "education": [{"end date": "Jun 2028"}],
        "certifications": [{"name": "AWS"}],
        "projects": [{"name": "Thing"}],
        "recommendations": [{"text": "Great to work with."}],
    }
    review_complete = linkedin_import.profile_review(
        review_complete_profile, tables=review_complete_tables)
    check("profile_review: a fully-populated profile has nothing missing",
          lambda: review_complete["official"]["missing"] == []
          or review_complete["official"]["missing"])
    check("profile_review: photo can never be checked, even from a complete export - "
          "no code path here ever opens image data",
          lambda: review_complete["official"]["unknown"] == ["Profile photo"]
          or review_complete["official"]["unknown"])
    check("profile_review: a fully-populated profile scores 6 of the 7 (photo excepted)",
          lambda: review_complete["official"]["present_count"] == 6
          or review_complete["official"]["present_count"])
    check("profile_review: total is always 7, matching LinkedIn's own framework",
          lambda: review_complete["official"]["total"] == 7
          or review_complete["official"]["total"])

    review_sparse = linkedin_import.profile_review(
        {"source": "linkedin_export", "skills": []})
    check("profile_review: a sparse profile scores lower",
          lambda: review_sparse["official"]["present_count"]
                  < review_complete["official"]["present_count"]
          or "sparse profile did not score lower than the complete one")
    check("profile_review: a sparse profile names what is missing",
          lambda: {"Location", "Industry", "Education",
                   "Position (work experience)", "Skills"}
                  <= set(review_sparse["official"]["missing"])
          or review_sparse["official"]["missing"])

    review_empty = linkedin_import.profile_review({})
    check("profile_review: an empty profile does not crash and has sensible defaults",
          lambda: (review_empty["official"]["present_count"] == 0
                   and review_empty["official"]["total"] == 7
                   and isinstance(review_empty["quality_tips"], list))
          or review_empty)

    review_pdf = linkedin_import.profile_review(
        {"source": "linkedin_pdf", "skills": ["python"], "recent_titles": ["Intern"]})
    check("profile_review: the PDF/text path does not crash on a minimal profile",
          lambda: isinstance(review_pdf["official"]["sections"], list) or review_pdf)
    check("profile_review: the PDF path marks Location unknown rather than 'missing' - "
          "derive_from_text has no field for it, so 'missing' would be an invented claim",
          lambda: next(s["present"] for s in review_pdf["official"]["sections"]
                       if s["name"] == "Location") is None
          or "PDF-derived profile claimed to know its own location")
    check("profile_review: the PDF path flags the reduced-fidelity import source",
          lambda: any(t["label"] == "Import source" for t in review_pdf["quality_tips"])
          or review_pdf["quality_tips"])
    check("profile_review: without tables, the export path is honest about the gap",
          lambda: bool(linkedin_import.profile_review(
              {"source": "linkedin_export"})["note"])
          or "no note about reduced fidelity")

    check("profile_review: an undescribed position is named as a tip",
          lambda: any("Position descriptions" in t["label"]
                     for t in linkedin_import.profile_review(
                         review_complete_profile,
                         tables={**review_complete_tables,
                                 "positions": [{"description": "Wrote Python."},
                                              {"description": ""}]})["quality_tips"])
          or "did not notice an undescribed role")
    check("profile_review: every position described means no such tip",
          lambda: not any("Position descriptions" in t["label"]
                         for t in review_complete["quality_tips"])
          or "nagged even though every role had a description")

    # A LinkedIn export with real PII sitting in exactly the free-text fields this
    # feature reads (Summary, position description, a recommendation) that the other
    # privacy checks above never touch - proving the boolean/count this function
    # returns never carries the text it was computed from.
    pii_fixture = ROOT / "out" / "_audit_review_export.zip"
    with _zipfile.ZipFile(pii_fixture, "w") as archive:
        archive.writestr(
            "Profile.csv",
            "First Name,Last Name,Summary,Industry,Geo Location\n"
            "Grace,Hopper,\"Reach me at grace@example.com or +44 7700 900321\","
            "Software,\"London, England\"\n")
        archive.writestr(
            "Positions.csv",
            "Company Name,Title,Description,Started On,Finished On\n"
            "Test Ltd,Intern,\"Call grace@example.com about this\",Jun 2025,Sep 2025\n")
        archive.writestr(
            "Recommendations_Received.csv",
            "First Name,Last Name,Text\nBob,Smith,\"Grace is great, reach her at "
            "grace@example.com\"\n")
        archive.writestr("Skills.csv", "Name\nPython\n")
    pii_tables = linkedin_import.read_export(pii_fixture)
    pii_profile = linkedin_import.derive(pii_tables)
    pii_review = linkedin_import.profile_review(pii_profile, tables=pii_tables)
    pii_blob = _json.dumps(pii_review)
    check("profile_review: reads a real Summary as present without keeping its text",
          lambda: (next(s["present"] for s in pii_review["official"]["sections"]
                       if s["name"] == "Summary") is True
                   and "grace@example.com" not in pii_blob)
          or "Summary check either missed real text or leaked it")
    check("profile_review: never leaks a name, even one sitting right in Summary/"
          "description/recommendation text",
          lambda: ("Grace" not in pii_blob and "Hopper" not in pii_blob
                   and "Bob" not in pii_blob and "Smith" not in pii_blob)
          or f"name leaked into {pii_blob[:200]}")
    check("profile_review: never leaks an email or phone from that same text",
          lambda: ("grace@example.com" not in pii_blob and "7700" not in pii_blob)
          or "email or phone leaked")
    check("profile_review: the scrubber agrees nothing identifying survived",
          lambda: privacy.contains_identifier(pii_blob) == []
          or privacy.contains_identifier(pii_blob))
    check("profile_review: a bonus recommendation shows only as a count",
          lambda: (any("Recommendations received" in t["label"]
                      for t in pii_review["quality_tips"])
                   and "grace@example.com" not in pii_blob)
          or "recommendation tip missing or leaked its text")
    if pii_fixture.exists():
        pii_fixture.unlink()

    # ---- the profile object itself cannot carry an identifier
    check("CVProfile: has no name/email/raw-text field",
          lambda: not ({"name", "email", "raw_text", "source_file"}
                       & {f.name for f in CVProfile.__dataclass_fields__.values()})
          or "CVProfile grew a personal field")
    check("CVProfile: to_dict drops anything not allowlisted",
          lambda: set(CVProfile(skills=["python"]).to_dict())
                  - privacy.PROFILE_ALLOWLIST - {"career_stage"} == set()
          or "to_dict leaked a key")

    # ---- the scrubber, on the cases that actually bite
    check("scrub: leaves an ISO timestamp alone",
          lambda: privacy.scrub("2026-09-21T03:04:12") == "2026-09-21T03:04:12"
          or privacy.scrub("2026-09-21T03:04:12"))
    check("scrub: leaves a salary alone",
          lambda: "32,000" in privacy.scrub("salary 32,000 to 38,000")
          or privacy.scrub("salary 32,000 to 38,000"))
    check("scrub: leaves a graduation year alone",
          lambda: "2029" in privacy.scrub("graduating 2029") or "year removed")
    check("scrub: removes a phone number",
          lambda: "7700" not in privacy.scrub("call +44 7700 900123") or "phone kept")
    check("scrub: removes a home directory path",
          lambda: "test" not in privacy.scrub(r"C:\Users\test\cv.pdf").lower()
          or privacy.scrub(r"C:\Users\test\cv.pdf"))

    # ---- tracker: the one column that could carry an identifier
    check("tracker: strips an email from an apply URL",
          lambda: "@" not in safe_url("https://x.com/a?id=1&email=me%40y.com")
          or safe_url("https://x.com/a?id=1&email=me%40y.com"))
    check("tracker: keeps the parameters the employer needs",
          lambda: "gh_jid=7" in safe_url("https://x.com/a?gh_jid=7&utm_source=li")
          or safe_url("https://x.com/a?gh_jid=7&utm_source=li"))
    check("tracker: leaves a URL with no query string alone",
          lambda: safe_url("https://x.com/jobs/7") == "https://x.com/jobs/7"
          or safe_url("https://x.com/jobs/7"))
    check("tracker: no column holds anything about the user",
          lambda: not ({"name", "email", "phone", "address", "candidate"}
                       & {h.lower() for h in tracker.HEADERS})
          or "a personal column appeared in the tracker")

    # ---- accounts
    audit_db = ROOT / "out" / "_audit_accounts.db"
    audit_accounts_dir = ROOT / "out" / "_audit_accounts"
    if audit_db.exists():
        audit_db.unlink()
    acc = Accounts(audit_db, accounts_dir=audit_accounts_dir)
    check("accounts: a test Accounts never writes into the real data/accounts/",
          lambda: acc.directory("x").parent == audit_accounts_dir
          or f"per-account folders would land in {acc.directory('x').parent}")
    from jobscout import accounts as _accounts_mod
    check("accounts: the real app's folders are still data/accounts/",
          lambda: _accounts_mod.Accounts.__init__.__defaults__[0] == _accounts_mod.DB_PATH
          and (_accounts_mod.DB_PATH.parent / "accounts") == _accounts_mod.ACCOUNTS_DIR
          or "the default location moved - existing users' saved criteria would vanish")
    check("accounts: refuse an email as a username",
          lambda: _raises(AccountError, acc.create, "me@x.com", "longenough1"))
    check("accounts: refuse a short password",
          lambda: _raises(AccountError, acc.create, "someone", "short"))
    made = acc.create("auditor", "correct horse battery")
    check("accounts: right password verifies",
          lambda: acc.verify("auditor", "correct horse battery") is not None
          or "verify failed")
    check("accounts: wrong password does not",
          lambda: acc.verify("auditor", "wrong") is None or "wrong password accepted")
    check("accounts: password is not stored in the clear",
          lambda: "correct horse battery" not in audit_db.read_bytes().decode(
              "latin-1") or "PASSWORD STORED IN PLAIN TEXT")
    check("accounts: no email column exists",
          lambda: "email" not in {r[1] for r in acc.conn.execute(
              "PRAGMA table_info(accounts)")} or "accounts table grew an email column")
    acc.save_profile(made["id"], dict(imported, first_name="Ada", email="a@b.com"))
    saved = acc.profile_path(made["id"]).read_text("utf-8")
    check("accounts: saving filters again on the way in",
          lambda: ("Ada" not in saved and "a@b.com" not in saved)
          or "save_profile stored a field it was handed")
    check("accounts: forgetting the profile removes the file",
          lambda: acc.forget_profile(made["id"])
                  and not acc.profile_path(made["id"]).exists()
          or "profile file survived")
    check("accounts: a stale or malformed cookie id is simply signed out",
          lambda: all(acc.get(bad) is None
                      for bad in (None, 123, "", "not-an-id", "x" * 64, {"a": 1}))
          or "a bad session id reached sqlite")

    # A second (or later) account signing up on the same install used to transparently
    # read, and on first save inherit, whatever the shared config.yaml currently held -
    # which on a machine used by more than one person is whoever last edited it signed
    # out, not reliably the new account holder. Exercised through the real Flask app
    # and a throwaway Store/Accounts pair, because the bug lived in app.py's cfg() and
    # api_account_create(), not in the jobscout package the rest of this file imports.
    def _account_isolation() -> object:
        import shutil as _shutil
        import tempfile as _tempfile
        _tmp = Path(_tempfile.mkdtemp())
        try:
            import app as _app
            saved_accounts, saved_store = _app.ACCOUNTS, _app.store
            _app.ACCOUNTS = Accounts(_tmp / "accounts.db")
            # The store has to be swapped too, not just saved and restored: every
            # /api/config save below rescores, and against the real store that rewrote
            # the user's actual scores and hidden-flags with these throwaway accounts'
            # blank criteria on every audit run.
            _app.store = Store(_tmp / "jobs.db")
            try:
                _app.PENDING.clear()
                c1, c2, c3 = (_app.app.test_client() for _ in range(3))
                c1.post("/api/account/create",
                        json={"username": "audit_a", "password": "auditpassA1"})
                c1.post("/api/config", json={"search": {"locations": ["First City"]}})
                c2.post("/api/account/create",
                        json={"username": "audit_b", "password": "auditpassB1"})
                second_sees = c2.get("/api/config").json["search"]["locations"]
                c3.post("/api/account/create",
                        json={"username": "audit_c", "password": "auditpassC1"})
                c2.post("/api/config", json={"search": {"locations": ["Second City"]}})
                third_after = c3.get("/api/config").json["search"]["locations"]
                first_after = c1.get("/api/config").json["search"]["locations"]
                if second_sees:
                    return f"a brand-new second account already saw {second_sees}"
                if third_after:
                    return f"a third account was affected by another's save: {third_after}"
                if first_after != ["First City"]:
                    return f"the first account's own criteria changed: {first_after}"
                return True
            finally:
                _app.store.close()
                _app.ACCOUNTS.close()
                _app.ACCOUNTS, _app.store = saved_accounts, saved_store
        finally:
            _shutil.rmtree(_tmp, ignore_errors=True)

    check("accounts: a second account never inherits another's criteria",
          _account_isolation)

    # Flask serves each request on its own thread, and the page opens seven endpoints at
    # once. A shared connection survived that in testing and failed in a browser, so the
    # check drives it the way the browser does.
    def _concurrent() -> object:
        import threading as _threading
        problems = []

        def worker() -> None:
            for _ in range(25):
                try:
                    acc.count()
                    store.stats()
                    store.query(limit=5)
                except Exception as exc:
                    problems.append(f"{type(exc).__name__}: {exc}")

        threads = [_threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return True if not problems else problems[:3]

    check("store and accounts survive parallel requests", _concurrent)
    acc.close()

    # =====================================================================
    #  RESEARCH-DRIVEN FIXES - work-mode honesty, staleness, salary, follow-ups
    # =====================================================================
    from datetime import datetime, timedelta, timezone
    from jobscout.scoring import work_mode_from_text

    check("work_mode: catches hybrid dressed as remote",
          lambda: work_mode_from_text(
              "Rider Support Representative", "London",
              "This role requires 3 days in the office each week.", True) == "hybrid"
          or "did not catch the Deliveroo-shaped case this was written for")
    check("work_mode: catches onsite dressed as remote",
          lambda: work_mode_from_text(
              "Store Assistant", "London", "This is an on-site role based in store.",
              True) == "onsite"
          or "did not catch an onsite role flagged remote")
    check("work_mode: a genuinely remote posting is not second-guessed",
          lambda: work_mode_from_text(
              "Backend Engineer", "Remote (UK)", "Fully remote, work from anywhere.",
              True) == "remote"
          or "flagged a real remote job as something else")
    check("work_mode: a silent posting falls back to the source's own flag",
          lambda: work_mode_from_text("Analyst", "London", "", True) == "remote"
          or "ignored the source flag with nothing else to go on")

    audit_jobs = ROOT / "out" / "_audit_jobs.db"
    if audit_jobs.exists():
        audit_jobs.unlink()
    jstore = Store(audit_jobs)
    old_stamp = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat(timespec="seconds")
    recent_stamp = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    old_posted = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat(timespec="seconds")
    fresh_posted = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")

    quiet_job = Job(source="s", source_kind="ats_direct", company="Quiet Co",
                    title="Analyst", url="https://x.com/1", posted_at=old_posted)
    fresh_job = Job(source="s", source_kind="ats_direct", company="Fresh Co",
                    title="Analyst", url="https://x.com/2", posted_at=fresh_posted,
                    salary_min=30000, salary_currency="GBP")
    silent_job = Job(source="s", source_kind="ats_direct", company="Silent Co",
                     title="Analyst", url="https://x.com/3")
    jstore.upsert([quiet_job, fresh_job, silent_job])
    jstore.conn.execute("UPDATE jobs SET status='applied', status_changed_at=? "
                        "WHERE company='Quiet Co'", (old_stamp,))
    jstore.conn.execute("UPDATE jobs SET status='applied', status_changed_at=? "
                        "WHERE company='Fresh Co'", (recent_stamp,))
    jstore.conn.commit()

    check("long_open: a 50-day-old posting is flagged",
          lambda: jstore.get(quiet_job.id)["long_open"] is True or "not flagged")
    check("long_open: a 5-day-old posting is not",
          lambda: jstore.get(fresh_job.id)["long_open"] is False or "false positive")
    check("salary_disclosed: a job with a real salary is disclosed",
          lambda: jstore.get(fresh_job.id)["salary_disclosed"] is True or "missed it")
    check("salary_disclosed: a job with nothing stated is not",
          lambda: jstore.get(silent_job.id)["salary_disclosed"] is False
          or "invented a salary")
    check("follow_ups: a quiet 25-day application is due",
          lambda: quiet_job.id in {j["id"] for j in jstore.follow_ups()}
          or "the 25-day-quiet application was not surfaced")
    check("follow_ups: a fresh 5-day application is not due yet",
          lambda: fresh_job.id not in {j["id"] for j in jstore.follow_ups()}
          or "nagged about an application only 5 days old")
    check("follow_ups: a job never applied to is never due",
          lambda: silent_job.id not in {j["id"] for j in jstore.follow_ups()}
          or "surfaced a job that was never applied to")
    check("stats: follow_up_due and salary_undisclosed agree with follow_ups()/query()",
          lambda: (jstore.stats()["follow_up_due"] == len(jstore.follow_ups())
                   and jstore.stats()["salary_undisclosed"] ==
                   len(jstore.query(limit=1000)) - len(
                       jstore.query(limit=1000, salary_disclosed_only=True)))
          or "stats() drifted from the methods it is meant to summarise")

    # ---- viewed tracking ("show what I've already opened")
    check("viewed: a posting nobody has opened reads as unviewed",
          lambda: jstore.get(silent_job.id)["viewed"] is False
          or "reads as viewed before anyone opened it")
    jstore.mark_viewed(silent_job.id)
    _first_view = jstore.get(silent_job.id)["viewed_at"]
    check("viewed: opening it stamps viewed / viewed_at",
          lambda: (jstore.get(silent_job.id)["viewed"] is True and bool(_first_view))
          or "mark_viewed did not record the view")
    jstore.conn.execute("UPDATE jobs SET viewed_at = '2020-01-01T00:00:00+00:00' "
                        "WHERE id = ?", (silent_job.id,))
    jstore.conn.commit()
    jstore.mark_viewed(silent_job.id)
    check("viewed: a second open keeps the FIRST view time, never overwrites it",
          lambda: jstore.get(silent_job.id)["viewed_at"].startswith("2020-01-01")
          or "a repeat open overwrote the first-seen timestamp")
    check("viewed: unviewed_only hides opened postings and keeps the rest",
          lambda: (lambda ids: silent_job.id not in ids and quiet_job.id in ids
                   and fresh_job.id in ids)(
              {j["id"] for j in jstore.query(limit=1000, unviewed_only=True)})
          or "the 'hide ones I've opened' filter is wrong")
    check("viewed: marking a job that does not exist is a silent no-op",
          lambda: jstore.mark_viewed("no-such-id") is None or "raised or returned junk")
    jstore.close()
    if audit_jobs.exists():
        audit_jobs.unlink()

    # =====================================================================
    #  CV AND COVER LETTER REVIEW - zero-fabrication discipline
    # =====================================================================
    from jobscout import review as review_mod
    from jobscout.cv import CVProfile as _CVProfile

    _review_job = Job(source="s", source_kind="ats_direct", company="Acme",
                      title="Graduate Software Engineer", url="https://x.com/1",
                      description="We need Python, SQL and Docker experience for "
                                  "this graduate role.")
    _review_profile = _CVProfile(skills=["python", "sql"], source="linkedin_export")

    _normal_cv = ("John Smith\njohn@example.com | 07700 900000\n\nEDUCATION\n"
                 "MEng Mechanical Engineering, University X, 2022-2026\n\n"
                 "EXPERIENCE\n- Built a test rig for a prosthetic hand using "
                 "SolidWorks and Arduino\n- Analysed sensor data in Python\n- Led a "
                 "group project on renewable energy systems\n\nSKILLS\nPython, "
                 "SolidWorks, CAD, Excel, teamwork\n")
    _garbled_cv = "\n".join(["Skills", "Python", "Experience", "SolidWorks", "Manager",
                            "Data", "Excel", "Team", "CAD", "Lead", "2023", "Intern",
                            "Sales", "UK", "Retail", "Tech", "Bristol", "BSc",
                            "Physics", "A-Level"])

    check("review: matches a skill the CV and the job both name",
          lambda: "python" in review_mod._skill_fit(_review_job, _review_profile, "")[0]
          or "missed an obvious match")
    check("review: flags a skill the job wants that the CV lacks, by name",
          lambda: "docker" in [m["skill"] for m in
                               review_mod._skill_fit(_review_job, _review_profile, "")[1]]
          or "missed an obvious gap")
    check("review: a well-formed CV with section headers is not flagged as garbled",
          lambda: next(c for c in review_mod._structure_checks(_normal_cv)
                      if c["check"] == "Reads in a sensible order")["ok"] is True
          or "false positive on headers - the exact bug found and fixed this session")
    check("review: a genuinely fragment-heavy extraction is still caught",
          lambda: next(c for c in review_mod._structure_checks(_garbled_cv)
                      if c["check"] == "Reads in a sensible order")["ok"] is False
          or "the garbled-extraction case stopped firing")
    check("review: recognises real section headers",
          lambda: next(c for c in review_mod._structure_checks(_normal_cv)
                      if c["check"] == "Recognisable sections")["ok"] is True
          or "missed EDUCATION/EXPERIENCE/SKILLS")

    # The fact-checking pass: this is the one check in this whole block that matters
    # most. A genuine quote must be accepted; a fabricated one - the exact shape an
    # ungrounded model response would produce - must be rejected, every time.
    _sources = {"cv": _normal_cv, "job": _review_job.description, "letter": ""}
    check("review: fact-check accepts a real, verbatim quote",
          lambda: review_mod._verify_evidence(
              "Built a test rig for a prosthetic hand", _sources) == "cv"
          or "rejected a genuine citation")
    check("review: fact-check rejects a fabricated claim",
          lambda: review_mod._verify_evidence(
              "Led the entire engineering division of 200 people", _sources) is None
          or "ACCEPTED A FABRICATED CLAIM - this is the one thing this feature must "
             "never do")
    check("review: fact-check is whitespace/case tolerant but not a rubber stamp",
          lambda: (review_mod._verify_evidence("  BUILT a test   rig", _sources) == "cv"
                   and review_mod._verify_evidence("built a spaceship", _sources) is None)
          or "tolerance logic is either too strict or too loose")
    check("review: an empty or tiny evidence string is never accepted",
          lambda: review_mod._verify_evidence("", _sources) is None
                  and review_mod._verify_evidence("a", _sources) is None
          or "accepted evidence too short to mean anything")

    check("review: build_review degrades cleanly with no CV file configured",
          lambda: bool((lambda r: not r.structure and r.has_cv and r.caveats)(
              review_mod.build_review(_review_job, _review_profile, Config(),
                                      use_model=False)))
          or "crashed or hid the no-CV caveat")
    check("review: build_review never crashes with profile=None",
          lambda: review_mod.build_review(_review_job, None, Config(),
                                          use_model=False) is not None
          or "crashed with no profile at all")
    check("review: cover letter voice-checking reuses humanise, not a reimplementation",
          lambda: len(review_mod.build_review(
              _review_job, _review_profile, Config(),
              cover_letter="I am passionate about leveraging cutting-edge synergies "
                           "to delve into robust solutions for this exciting "
                           "opportunity in a fast-paced environment.",
              use_model=False).voice_tells) >= 3
          or "did not catch an obviously generated letter")
    check("review: every hardcoded resource link is genuinely live right now",
          lambda: _dead_links([r["url"] for r in review_mod.RESOURCES]))

    # No new PII field anywhere near this feature - CV text and cover letters are read
    # for the length of one request and returned to the browser, never written to disk,
    # profile.json, or a log line.
    import json as _json
    _pii_job = Job(source="s", source_kind="ats_direct", company="Acme", title="T",
                  url="https://x.com/1",
                  description="Contact Jane Doe at jane.doe@example.com if interested.")
    _pii_review = review_mod.build_review(
        _pii_job, _review_profile, Config(),
        cover_letter="My name is Jane Doe, reach me on jane.doe@example.com or "
                     "+44 7700 900123.",
        use_model=False)
    _pii_blob = _json.dumps(_pii_review.to_dict())
    check("review: PII passed through it never leaks into the response",
          lambda: (privacy.contains_identifier(_pii_blob) == []
                   and "jane.doe@example.com" not in _pii_blob
                   and "7700 900123" not in _pii_blob)
          or f"leaked: {privacy.contains_identifier(_pii_blob)}")
    _review_source = Path(review_mod.__file__).read_text("utf-8")
    check("review: the module itself never writes CV/letter text to disk",
          lambda: not any(pattern in _review_source
                          for pattern in ("write_text(", ".write(cv", ".write(letter"))
          or "review.py contains a disk-write call - it must not persist CV/letter text")

    # =====================================================================
    #  SECURITY HARDENING - headers, rate limiting, the checklist in
    #  Web & Marketing/Launch Checklist — App Security.md
    # =====================================================================
    import re as _sec_re
    import app as _security_app
    _sc = _security_app.app.test_client()

    _headers = _sc.get("/").headers
    check("security: CSP is present and same-origin by default",
          lambda: "default-src 'self'" in (_headers.get("Content-Security-Policy") or "")
          or "no restrictive default-src")
    check("security: CSP allows no script source but self - no inline, no CDN",
          lambda: "script-src 'self'" in (_headers.get("Content-Security-Policy") or "")
          or "script-src is missing or permissive")
    check("security: clickjacking header present",
          lambda: _headers.get("X-Frame-Options") == "DENY" or "framing not blocked")
    check("security: MIME-sniffing header present",
          lambda: _headers.get("X-Content-Type-Options") == "nosniff"
          or "browsers may still guess content types")
    check("security: no HSTS header on a plain-HTTP local server",
          lambda: "Strict-Transport-Security" not in _headers
          or "HSTS claims a transport this app does not have")

    check("security: no CORS library imported anywhere",
          lambda: not any("flask_cors" in Path(f).read_text("utf-8", errors="ignore")
                          for f in [ROOT / "app.py"] + list((ROOT / "jobscout").glob("*.py")))
          or "flask-cors is imported - check it is not set to allow_origins='*'")

    # Rate limiting: allows the configured ceiling, then blocks, keyed by username so
    # a lockout on one account cannot be used to lock a different one out too.
    for _ in range(_security_app.LOGIN_MAX_ATTEMPTS):
        _sc.post("/api/account/login", json={"username": "_audit_ratelimit_probe",
                                             "password": "wrong"})
    _blocked = _sc.post("/api/account/login",
                        json={"username": "_audit_ratelimit_probe", "password": "wrong"})
    _other_account = _sc.post("/api/account/login",
                              json={"username": "_audit_ratelimit_other", "password": "wrong"})
    check("security: login is rate limited after repeated failures",
          lambda: _blocked.status_code == 429 or "no lockout after the configured ceiling")
    check("security: a lockout on one username does not affect a different one",
          lambda: _other_account.status_code == 401
          or "one account's lockout blocked an unrelated account")
    _security_app._clear_login_failures("_audit_ratelimit_probe")
    _security_app._clear_login_failures("_audit_ratelimit_other")

    # The four causes of real breaches this checklist names, checked directly against
    # this codebase rather than assumed: no ORM/RLS here (SQLite, single install) so
    # the equivalent question is parameterisation, which store.py's query() builder
    # already keeps to placeholder-only interpolation - this locks that in.
    _store_source = (ROOT / "jobscout" / "store.py").read_text("utf-8")
    _fstring_sql = _sec_re.findall(r'f"[^"]*(?:SELECT|INSERT|UPDATE|DELETE)[^"]*"', _store_source, _sec_re.I)
    check("security: no client value is ever concatenated into SQL text",
          lambda: all(("{" not in s) or all(
              # "where" is Store._where()'s output and "marks" a run of "?" - placeholders only.
              tok in ("column", "slots", "blank", "kind_slots", "clause", "visible", "where", "marks")
              for tok in _sec_re.findall(r"\{(\w+)\}", s)) for s in _fstring_sql)
          or f"an f-string SQL fragment interpolates something unexpected: {_fstring_sql}")

    check("security: no API key or token referenced from any static JS file",
          lambda: not any(
              _sec_re.search(r"api[_-]?key|anthropic|secret",
                             (ROOT / "static" / f).read_text("utf-8"), _sec_re.I)
              for f in ("app.js", "deck.js", "profile.js"))
          or "a secret-shaped identifier appears in client-side JS")
    check("security: nothing is ever written to localStorage/sessionStorage",
          lambda: not any(
              "localStorage" in (ROOT / "static" / f).read_text("utf-8")
              or "sessionStorage" in (ROOT / "static" / f).read_text("utf-8")
              for f in ("app.js", "deck.js", "profile.js"))
          or "a client-side storage API is in use - auth state must stay in the "
             "HttpOnly session cookie only")

    _app_source = (ROOT / "app.py").read_text("utf-8")
    check("security: session cookie is HttpOnly and SameSite, set once at startup",
          lambda: "SESSION_COOKIE_HTTPONLY=True" in _app_source
                  and "SESSION_COOKIE_SAMESITE" in _app_source
          or "session cookie flags are missing or were removed")
    check("security: the server binds to loopback only, not every interface",
          lambda: 'host="127.0.0.1"' in _app_source
          or "app.run binds somewhere other than 127.0.0.1 - this exposes it on the "
             "network, which changes every threat-model assumption in this section")
    check("security: debug mode is off",
          lambda: "debug=False" in _app_source
          or "Flask debug mode is on - this exposes the interactive debugger, which "
             "is a remote code execution risk if this port is ever reachable by anyone else")

    # =====================================================================
    #  CUSTOM RULES
    # =====================================================================
    def _job(**kw):
        base = dict(source="t", source_kind="ats_direct", company="Acme",
                    title="Warehouse Operative", url="u", location="Stratford",
                    description="Night shift rota in a pub kitchen")
        base.update(kw)
        return Job(**base)

    parsed = rules_mod.parse([
        {"label": "No nights", "terms": ["night shift"], "mode": "exclude"},
        {"label": "Near me", "terms": ["stratford"], "mode": "boost",
         "field": "location", "weight": 12, "facet": True},
        {"label": "", "terms": ["x"]},
        {"label": "No terms", "terms": []},
        {"label": "NO NIGHTS", "terms": ["overnight"], "mode": "exclude"},
    ])
    check("rules: drop the malformed ones",
          lambda: len(parsed) == 2 or [r.label for r in parsed])
    check("rules: same label does not make two rules",
          lambda: len({r.key for r in parsed}) == len(parsed)
          or [r.key for r in parsed])
    check("rules: last rule with a label wins",
          lambda: parsed[0].terms == ["overnight"] or parsed[0].terms)
    check("rules: exclude actually excludes",
          lambda: bool(rules_mod.hard_filter(
              rules_mod.parse([{"label": "n", "terms": ["night shift"],
                                "mode": "exclude"}]), _job()))
          or "exclude rule did not fire")
    check("rules: require drops what does not match",
          lambda: bool(rules_mod.hard_filter(
              rules_mod.parse([{"label": "r", "terms": ["solidworks"],
                                "mode": "require"}]), _job()))
          or "require rule did not fire")
    check("rules: boost adds points and says why",
          lambda: rules_mod.score(parsed, _job())[0] == 12
          or rules_mod.score(parsed, _job()))
    check("rules: word boundaries, so 'pub' misses 'Global Public Sector'",
          lambda: not rules_mod.matches(
              rules_mod.parse([{"label": "p", "terms": ["pub"]}])[0],
              _job(description="Global Public Sector team", title="Analyst"))
          or "substring match - the Public Sector bug is back")
    check("rules: a trailing star still matches loosely",
          lambda: rules_mod.matches(
              rules_mod.parse([{"label": "p", "terms": ["prosthe*"]}])[0],
              _job(description="prosthetics laboratory")) == "prosthetics"
          or "wildcard term did not match")
    check("rules: the SQL clause and the regex agree on an obvious hit",
          lambda: ("title LIKE ?" in rules_mod.sql_clause(
              rules_mod.parse([{"label": "t", "terms": ["warehouse"],
                                "field": "title"}])[0])[0])
          or "sql_clause scoped to the wrong column")

    # =====================================================================
    #  INTERVIEW PREP
    # =====================================================================
    check("interview: finds the stage an advert names",
          lambda: "Coding assessment" in [
              s["stage"] for s in interview.detect_assessments(
                  _job(description="You will sit a HackerRank coding test"))]
          or "named stage missed")
    check("interview: does not invent stages",
          lambda: interview.detect_assessments(
              _job(description="A friendly team in a nice office")) == []
          or "stage invented from nothing")
    check("interview: maps a known employer to its provider",
          lambda: "Watson Glaser" in [p["name"]
                                      for p in interview.providers_for("Linklaters")]
          or "employer-to-provider map broken")
    check("interview: says nothing for an unknown employer",
          lambda: interview.providers_for("Some Tiny Startup Ltd") == []
          or "provider guessed for an unknown employer")
    check("interview: employer practice packs resolve through legal suffixes",
          lambda: len(interview.employer_packs("Amazon UK Services Limited")) >= 1
          or "AssessmentDay index not built - run tools/discover_assessments.py")
    check("interview: every provider link is absolute",
          lambda: all(url.startswith("https://")
                      for p in interview.PROVIDERS.values()
                      for _, url, _ in p["practice"] if url)
          or "a relative or empty provider URL")
    check("interview: values questions come from stated values",
          lambda: "Act like an owner" in interview.values_questions(
              ["Act like an owner"], "Acme")[0]["question"]
          or "values question not built from the value")

    # =====================================================================
    #  HUMAN VOICE
    # =====================================================================
    robot = ("I am passionate about leveraging cutting-edge solutions. Moreover, this "
             "is a testament to my meticulous approach. Furthermore, I am thrilled to "
             "delve into this myriad of opportunities.")
    check("humanise: catches the obvious tells",
          lambda: len(humanise.tells(robot)) >= 6 or humanise.tells(robot))
    check("humanise: scores obviously generated text low",
          lambda: humanise.score(robot) < 40 or humanise.score(robot))
    check("humanise: leaves plain writing alone",
          lambda: humanise.tells(
              "I built a test rig for a prosthetic hand. The strain gauge readings "
              "were noisy, so I rewrote the logger. It worked.") == []
          or "flagged ordinary prose")
    check("humanise: soften swaps the safe ones",
          lambda: "leverage" not in humanise.soften("We leverage data")
          or humanise.soften("We leverage data"))
    check("humanise: the voice rules ban the words the checker catches",
          lambda: all(word in humanise.VOICE_RULES
                      for word in ("delve", "leverage", "passionate about"))
          or "the prompt and the checker disagree")

    # =====================================================================
    #  SAVED FILTERS + VIEWED - the Positions sidebar survives a reload
    # =====================================================================
    import os as _os
    import inspect as _inspect
    import shutil as _shutil
    from jobscout.config import Config as _Cfg, ViewPrefs as _ViewPrefs

    _view_cfg_path = ROOT / "out" / "_audit_view_config.yaml"
    _real_cfg = ROOT / "config.yaml"
    if _real_cfg.exists():
        _shutil.copyfile(_real_cfg, _view_cfg_path)
    _real_cfg_bytes = _real_cfg.read_bytes() if _real_cfg.exists() else b""
    _prev_env = _os.environ.get("JOBSCOUT_CONFIG")
    _os.environ["JOBSCOUT_CONFIG"] = str(_view_cfg_path)
    try:
        _before_titles = list(_Cfg.load().search.titles)
        _before_order = _Cfg.load().view.order
        _vr = _sc.post("/api/view", json={
            "q": "graduate", "min_score": 250, "order": "DROP TABLE jobs",
            "status": "applied", "unviewed": True, "salary_disclosed": True,
            "employment": ["internship", "placement"], "families": ["engineering"],
            "rules": ["mine"]})
        _saved = _Cfg.load().view
        check("saved filters: /api/view accepts a save",
              lambda: _vr.status_code == 200 or f"HTTP {_vr.status_code}")
        check("saved filters: the save round-trips through the config file",
              lambda: (_saved.q == "graduate" and _saved.status == "applied"
                       and _saved.unviewed is True and _saved.salary_disclosed is True
                       and _saved.employment == ["internship", "placement"]
                       and _saved.families == ["engineering"] and _saved.rules == ["mine"])
              or f"came back as {_saved}")
        check("saved filters: min_score is clamped to 0-100, not stored as sent",
              lambda: _saved.min_score == 100 or f"stored {_saved.min_score}")
        check("saved filters: an unknown sort order is refused, keeping the previous one",
              lambda: _saved.order == _before_order
              or f"stored order={_saved.order!r}, was {_before_order!r}")
        check("saved filters: saving the view never touches search criteria",
              lambda: _Cfg.load().search.titles == _before_titles
              or "a sidebar save rewrote the Criteria tab's titles")
        check("saved filters: saving the view does not trigger a rescore",
              lambda: "rescore" not in (_vr.get_json() or {})
              or "the lightweight endpoint is doing the heavy rescore")
        check("saved filters: GET /api/config hands the saved view back to the page",
              lambda: (_sc.get("/api/config").get_json() or {}).get("view", {}).get("q")
                      == "graduate"
              or "the page has no way to restore the sidebar")
        check("saved filters: a junk body is handled, not a 500",
              lambda: _sc.post("/api/view", data="not json",
                               content_type="text/plain").status_code == 200
              or "a malformed save crashed the endpoint")
    finally:
        if _prev_env is None:
            _os.environ.pop("JOBSCOUT_CONFIG", None)
        else:
            _os.environ["JOBSCOUT_CONFIG"] = _prev_env
        if _view_cfg_path.exists():
            _view_cfg_path.unlink()
    check("saved filters: the audit left the real config.yaml byte-for-byte untouched",
          lambda: (_real_cfg.read_bytes() if _real_cfg.exists() else b"") == _real_cfg_bytes
          or "the audit wrote to the user's real config.yaml")

    # ---- "Run scrape" - the worker thread has no Flask session
    import time as _time
    _captured: dict = {}

    def _fake_pipeline(configuration, _store, _progress, profile):
        _captured["config_type"] = type(configuration).__name__
        return {"new": 0, "kept": 0, "found": 0}

    _real_pipeline = _security_app.run_pipeline
    _security_app.run_pipeline = _fake_pipeline
    _scrape_before = dict(_security_app.SCRAPE)
    try:
        with _security_app.app.test_request_context("/api/scrape", method="POST"):
            _launched = _security_app._start_scrape()
        _deadline = _time.time() + 10
        while _security_app.SCRAPE["running"] and _time.time() < _deadline:
            _time.sleep(0.05)
        _scrape_after = dict(_security_app.SCRAPE)
    finally:
        _security_app.run_pipeline = _real_pipeline
        _security_app.SCRAPE.clear()
        _security_app.SCRAPE.update(_scrape_before)
    check("scrape: a started scrape runs to completion instead of crashing",
          lambda: (_launched and _scrape_after.get("error") is None
                   and _captured.get("config_type") == "Config")
          or f"launched={_launched}, error={_scrape_after.get('error')!r}")
    check("scrape: the profile is never parked in the public /api/scrape/status payload",
          lambda: "profile" not in _scrape_after
          or "the signed-in person's profile is readable from the status endpoint")

    # ---- old run records predating privacy.py
    from jobscout import privacy as _privacy
    _runs_db = ROOT / "out" / "_audit_runs.db"
    if _runs_db.exists():
        _runs_db.unlink()
    _rs = Store(_runs_db)
    _rs.conn.execute(
        "INSERT INTO runs (started_at, finished_at, found, kept, new, detail) "
        "VALUES ('2026-01-01', '2026-01-01', 1, 1, 1, ?)",
        (_json.dumps({"cv": {"email": "someone@example.com", "linkedin_slug": "someone",
                            "source_file": "C:/Users/someone/cv.docx",
                            "raw_text": "Someone, someone@example.com, 07700 900000",
                            "skills": ["python"], "graduation_year": "2029"}}),))
    _rs.conn.commit()
    _rs.close()
    _rs = Store(_runs_db)                      # reopening runs the migration
    _old_detail = _json.loads(_rs.stats()["last_run"]["detail"])
    _rs.close()
    _runs_db.unlink()
    check("privacy: an old run record's email/slug/path/raw text are scrubbed on startup",
          lambda: not ({"email", "linkedin_slug", "source_file", "raw_text"}
                       & set(_old_detail["cv"]))
          and not _privacy.contains_identifier(_json.dumps(_old_detail))
          or f"still holds {sorted(set(_old_detail['cv']))}")
    check("privacy: the scrub keeps the allowlisted, non-identifying parts",
          lambda: _old_detail["cv"].get("skills") == ["python"]
          and _old_detail["cv"].get("graduation_year") == "2029"
          or "the scrub threw away useful, allowlisted data too")

    # ---- company stability rating
    from jobscout import stability as _stab
    from datetime import date as _date
    _today = _date(2026, 9, 28)
    _wd_confirmed = {"source": "wikidata", "name": "Dyson", "domain": "dyson.com",
                     "employees": 14000, "founded": "1991", "wikidata_id": "Q1"}
    _r_est = _stab.assess("Dyson", profile=_wd_confirmed, sponsor_rating="A (SME+)",
                          job_url="https://careers.dyson.com/job/1", tags={},
                          register=None, activity={"open_roles": 40, "long_open": 2},
                          today=_today)
    check("stability: a big, old, A-rated, actively hiring employer reads Established",
          lambda: _r_est["level"] == "established" or f"got {_r_est['level']}")
    check("stability: every factor names its source",
          lambda: all(f["source"] for f in _r_est["factors"]) or "a factor has no source")
    _r_dead = _stab.assess("Gone Ltd", profile=None, tags={}, today=_today,
                           register={"company_number": "01234567",
                                     "company_status": "liquidation",
                                     "date_of_creation": "1990-01-01"})
    check("stability: a company in liquidation is flagged whatever else is true",
          lambda: _r_dead["level"] == "caution" or f"got {_r_dead['level']}")
    _namesake = {"source": "wikidata", "name": "Acme Corporation", "domain": "acme.com",
                 "employees": 90000, "founded": "1900", "wikidata_id": "Q2"}
    _r_ns = _stab.assess("Acme Robotics", profile=_namesake, tags={}, today=_today,
                         job_url="https://jobs.lever.co/acmerobotics/1")
    check("stability: an unconfirmed Wikidata namesake's facts are never used",
          lambda: (_r_ns["level"] == "unknown"
                   and not any(f["source"] == "Wikidata" for f in _r_ns["factors"]))
          or f"used a namesake's facts: {_r_ns['factors']}")
    _r_one = _stab.assess("Tiny Co", profile=None, sponsor_rating="A", tags={},
                          today=_today)
    check("stability: one weak signal alone gives 'not enough data', not a verdict",
          lambda: _r_one["level"] == "unknown" or f"got {_r_one['level']}")
    check("stability: the disclaimer says it is not employee reviews",
          lambda: "not employee reviews" in _r_one["disclaimer"] or "disclaimer changed")
    _r_su = _stab.assess("Seedco", profile=None, sponsor_rating="A", today=_today,
                         tags={"startup": True, "startup_evidence": "YC W24, 12 staff"})
    check("stability: a tagged startup reads Early-stage",
          lambda: _r_su["level"] == "early" or f"got {_r_su['level']}")
    _r_flint = _stab.assess("Flint", profile=None, sponsor_rating="A (SME+)",
                            sponsor_name="Flint Wines Ltd.", sponsor_confidence=0.55,
                            tags={"startup": True, "startup_evidence": "YC S20, 50 staff"},
                            today=_today)
    check("stability: an approximate sponsor-register match is never credited",
          lambda: (not any(f["source"] == "UK sponsor register" for f in _r_flint["factors"])
                   and any("approximate" in n for n in _r_flint["not_checked"]))
          or f"credited a namesake's licence: {_r_flint['factors']}")
    check("stability: a YC-tagged startup is Early-stage even with no other facts",
          lambda: _r_flint["level"] == "early" or f"got {_r_flint['level']}")
    check("stability: without a Companies House key, the gap is stated not hidden",
          lambda: _os.environ.get("COMPANIES_HOUSE_KEY")
          or any("COMPANIES_HOUSE_KEY" in n for n in _r_one["not_checked"])
          or "missing-key gap not reported")

    # ---- startup tags, the startup filter, and the extra company registry
    _stab.reset_tags()
    _flags = _stab.startup_flags()
    check("startups: registry tags load, including companies_extra.yaml",
          lambda: (not (ROOT / "data" / "companies_extra.yaml").exists())
          or sum(_flags.values()) > 0 or "no startup tags loaded")
    check("startups: the tags file's meta block is not read as a company",
          lambda: "meta" not in _flags and "tags" not in _flags
          or "loader treated file keys as company names")
    _su_db = ROOT / "out" / "_audit_startups.db"
    if _su_db.exists():
        _su_db.unlink()
    _ss = Store(_su_db)
    _ss.upsert([Job(source="s", source_kind="ats_direct", company="Seedco", title="Intern",
                    url="https://x.com/s1"),
                Job(source="s", source_kind="ats_direct", company="BigCorp", title="Intern",
                    url="https://x.com/s2"),
                Job(source="s", source_kind="ats_direct", company="Nobody Knows",
                    title="Intern", url="https://x.com/s3")])
    _ss.apply_startup_flags({"seedco": True, "bigcorp": False})
    _only = {j["company"] for j in _ss.query(limit=100, startups="only")}
    _hide = {j["company"] for j in _ss.query(limit=100, startups="hide")}
    _ss.close()
    _su_db.unlink()
    check("startups: 'only' shows tagged startups and nothing else",
          lambda: _only == {"Seedco"} or f"got {_only}")
    check("startups: 'hide' drops startups but keeps employers the registry knows nothing about",
          lambda: _hide == {"BigCorp", "Nobody Knows"} or f"got {_hide}")

    # ---- combined filters: sidebar counts, totals, paging, literal search
    _fc_db = ROOT / "out" / "_audit_facets.db"
    if _fc_db.exists():
        _fc_db.unlink()
    _fs = Store(_fc_db)
    _fs.upsert([Job(source="a" if i % 2 else "b", source_kind="ats_direct", company=f"Co{i}",
                    title="Data_Intern" if i == 0 else f"Role {i}",
                    # "1000 staff" and "a team" only match if % or _ act as wildcards
                    description={1: "100% remote", 2: "1000 staff"}.get(i, "a team"),
                    url=f"https://x.com/f{i}") for i in range(12)])
    for _i, (_kind, _fam) in enumerate([("internship", "software")] * 4
                                       + [("placement", "software")] * 3
                                       + [("placement", "finance")] * 5):
        _fs.conn.execute("UPDATE jobs SET employment_kind = ?, job_family = ? WHERE url = ?",
                         (_kind, _fam, f"https://x.com/f{_i}"))
    _fs.conn.commit()
    _narrow = dict(employment=["placement"], families=["finance"], include_unclassified=False)
    _facets = _fs.facet_counts(**_narrow)
    _total = _fs.count(**_narrow)
    _pages = [j["id"] for off in range(0, 12, 5)
              for j in _fs.query(limit=5, offset=off, order="closing")]
    _pct = [j["title"] for j in _fs.query(limit=50, search="100%")]
    _under = [j["title"] for j in _fs.query(limit=50, search="a_")]
    _fs.close()
    _fc_db.unlink()
    check("filters: a group's counts follow the OTHER groups' ticks (finance -> 5 placements)",
          lambda: _facets["employment"] == {"placement": 5}
          or f"employment counts ignore the field filter: {_facets['employment']}")
    check("filters: a group's counts ignore its own ticks, so it can still be widened",
          lambda: _facets["families"] == {"software": 3, "finance": 5}
          or f"got {_facets['families']}")
    check("filters: the total is every match, not just the page", lambda: _total == 5 or _total)
    check("filters: paging with tied sort keys never repeats or skips a posting",
          lambda: len(_pages) == 12 == len(set(_pages)) or f"{len(set(_pages))} of 12 distinct")
    check("filters: '%' and '_' in a search are literal text, not wildcards",
          lambda: (_pct == ["Role 1"] and _under == ["Data_Intern"]) or f"{_pct} {_under}")

    # ---- application tracker: import the user's own spreadsheet, status sync, export
    import io as _io
    import openpyxl as _oxl
    from jobscout import tracker as _trk, tracker_import as _ti

    def _book(rows):
        _wb = _oxl.Workbook()
        for _r in rows:
            _wb.active.append(_r)
        _buf = _io.BytesIO()
        _wb.save(_buf)
        return _buf.getvalue()

    _tr_db = ROOT / "out" / "_audit_tracker.db"
    if _tr_db.exists():
        _tr_db.unlink()
    _ts = Store(_tr_db)
    _ts.upsert([Job(source="greenhouse", source_kind="ats_direct", company="Acme Ltd",
                    title="Software Placement", url="https://boards.example/acme/1",
                    location="London", external_id="1")])
    _sheet = _book([
        ["Comapny", "Role Name", "Deadline", "Application Link", "Applied?",
         "Date Applied", "Status", "Notes", "Recruiter"],
        ["Acme", "Software Placement", "30/11/2026", "https://boards.example/acme/1", True,
         "20/09/2026", "Assessment centre", "went well", "Sam"],
        ["Globex", "Data Intern", "15/12/2026", "globex.example/jobs/9", False, None, None,
         None, None],
        ["Initech", "Quant Intern", None, None, "yes", None, "OA sent", None, None],
        ["Hooli", "SWE Intern", None, None, None, None, "Lunch with Gavin", None, None],
        [None, "Role with no company", None, None, None, None, None, None, None],
        [None, None, None, None, False, None, None, None, None],   # template filler
    ])
    _first = _ti.import_tracker(_ts, _sheet, "mine.xlsx")
    _again = _ti.import_tracker(_ts, _sheet, "mine.xlsx")
    _by = {j["company"]: j for j in _ts.tracked()}
    _rows_now = _ts.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    _exported = ROOT / "out" / "_audit_tracker.xlsx"
    _trk.export_new(_ts.tracked(), _exported)
    _round = _ti.import_tracker(_ts, _exported.read_bytes(), "export.xlsx")
    _exported.unlink()
    _acme_row = _trk.job_to_row(_by["Acme Ltd"])
    _gid = _by["Globex"]["id"]
    _ts.set_status(_gid, "interviewing")
    _s1 = _ts.get(_gid)["app_status"]
    _ts.set_app_status(_gid, "Final round")
    _ts.set_status(_gid, "interviewing")
    _s2 = _ts.get(_gid)["app_status"]
    _ts.set_status(_gid, "new")
    _off = _ts.get(_gid)
    _kept_scraped = not _ts.delete_manual(_by["Acme Ltd"]["id"])
    _gone_manual = _ts.delete_manual(_by["Hooli"]["id"])
    _ts.close()
    _tr_db.unlink()
    check("tracker import: a misspelt 'Comapny' header and a spreadsheet's own columns are read",
          lambda: _first["columns"].get("Comapny") == "company"
          and _first["kept_extra_columns"] == ["Recruiter"] or _first)
    check("tracker import: a row matching a scanned posting tracks that posting, no duplicate",
          lambda: (_first["added"], _first["updated"]) == (3, 1)
          and _by["Acme Ltd"]["source_kind"] == "ats_direct" or _first)
    check("tracker import: rows with no company are skipped, template filler silently",
          lambda: _first["skipped"] == 1 or _first)
    check("tracker import: importing the same file again changes nothing",
          lambda: (_again["added"], _rows_now) == (0, 4) or (_again, _rows_now))
    check("tracker import: statuses written loosely map to the workbook's stages",
          lambda: [_by[c]["app_status"] for c in ("Acme Ltd", "Globex", "Initech")]
          == ["2nd / AC", "Not applied yet", "Online test"]
          or [_by[c]["app_status"] for c in _by])
    check("tracker import: an unrecognised status is kept in the notes, not lost",
          lambda: "Lunch with Gavin" in _by["Hooli"]["notes"] or _by["Hooli"]["notes"])
    check("tracker import: the sheet's date applied is kept and none is invented",
          lambda: (_by["Acme Ltd"]["applied_at"], _by["Initech"]["applied_at"])
          == ("2026-09-20", "") or (_by["Acme Ltd"]["applied_at"], _by["Initech"]["applied_at"]))
    check("tracker import: dd/mm/yyyy deadlines and a bare domain link are understood",
          lambda: (_by["Globex"]["closes_at"], _by["Globex"]["url"])
          == ("2026-12-15", "https://globex.example/jobs/9") or _by["Globex"])
    check("tracker import: the app's own exported workbook imports back with no new rows",
          lambda: (_round["added"], _round["header_row"]) == (0, 2) or _round)
    check("tracker export: stage, applied flag, date applied and notes reach the workbook",
          lambda: (_acme_row[14], _acme_row[12], str(_acme_row[13]), _acme_row[18])
          == ("2nd / AC", True, "2026-09-20", "went well") or _acme_row)
    check("tracker: the jobs-list status and the tracker stage stay in step both ways",
          lambda: (_s1, _s2) == ("1st interview", "Final round") or (_s1, _s2))
    check("tracker: setting a job back to 'new' takes it off the tracker",
          lambda: not _off["tracked"] and _off["app_status"] == "" or _off)
    check("tracker: only rows the user added are deleted; scanned postings never are",
          lambda: _kept_scraped and _gone_manual or (_kept_scraped, _gone_manual))
    check("tracker: the in-app stages are exactly the workbook's Status dropdown",
          lambda: _trk.LISTS["lst_Status"] == __import__("jobscout.store", fromlist=["x"])
          .APP_STATUSES or "lists differ")

    from jobscout.pipeline import load_companies as _load_companies
    _registry = _load_companies(Config())
    _keys = [(c.get("ats"), str(c.get("slug", "")).lower()) for c in _registry]
    check("registry: companies_extra.yaml is merged without duplicate boards",
          lambda: len(_keys) == len(set(_keys)) or "the same board is listed twice")
    check("registry: every Workday entry has the tenant:wdN:site slug the adapter needs",
          lambda: all(_sec_re.fullmatch(r"[^:]+:wd\d+:[^:]+", str(c["slug"]))
                      for c in _registry if c.get("ats") == "workday")
          or "a Workday slug is malformed")

    # ---- query-ignoring sources are asked once with the whole budget
    from jobscout import pipeline as _pl
    _calls: list = []

    class _Echo:
        name, kind, needs_key, uses_query, markets = "echo", "board", False, False, ()
        def serves(self, home): return True
        def available(self): return True
        def fetch(self, query="", location="", max_results=0, home=None, **kw):
            _calls.append((query, max_results))
            return []

    _pl.ALL_ADAPTERS["echo"] = _Echo()
    try:
        _qc = Config()
        _qc.sources.aggregators = ["echo"]
        _qc.sources.aggregator_queries = ["a", "b", "c"]
        _qc.sources.max_per_source = 100
        _pl.gather_aggregators(_qc)
    finally:
        _pl.ALL_ADAPTERS.pop("echo", None)
    check("pipeline: a source that ignores the query is called once, with the full budget",
          lambda: _calls == [("", 300)] or f"calls were {_calls}")

    # ---- student focus
    check("student focus: a new install defaults to the student career stage",
          lambda: Config().search.career_stage == "student" or "default changed")
    _placement = Job(source="gradcracker", source_kind="board", company="X",
                     title="Mechanical Engineer", url="https://x.com/p",
                     employment_type="Placement/Internship")
    from jobscout.scoring import _level_adjust
    check("student focus: a board-labelled placement is boosted even with a plain title",
          lambda: _level_adjust(_placement, Config())[0] > 0
          or f"classified as {_placement.employment_kind!r}, adjust "
             f"{_level_adjust(_placement, Config())}")

    # ---- board labels classify (RateMyPlacement / Gradcracker / GitHub new-grad)
    def _kind(employment_type: str, title: str) -> str:
        return Job(source="s", source_kind="board", company="C", title=title, url="u",
                   employment_type=employment_type).employment_kind
    for _et, _title, _want in (("Placement (10 Months+)", "Mechanical Engineer", "placement"),
                               ("Placement/Internship", "Design Engineer", "placement"),
                               ("Placement/Internship", "Software Engineering Internship",
                                "internship"),
                               ("New grad", "Software Engineer", "graduate_scheme"),
                               ("", "Placement Officer", ""),
                               ("", "Recruitment Consultant - Placements", "")):
        check(f"classify: {_et or '(no label)'!r} + {_title!r} -> {_want or 'unclassified'}",
              lambda e=_et, t=_title, w=_want: _kind(e, t) == w
              or f"got {_kind(e, t)!r}")

    # ---- GitHub tracker parser: header-located columns, every link style
    from jobscout.sources import github_boards as _gb
    _md = "\n".join([
        "| Company | Role | Location | Apply | Date Posted |",
        "|---|---|---|---|---|",
        "| **[Acme](https://acme.example)** | [[2027] Software Engineer, Early Career]"
        "(https://jobs.example/acme/1) | London, UK | "
        "[![Apply](https://img.shields.io/badge/-Apply-blue)](https://acme.example/apply/1) | Sep 30 |",
        "| ↳ | Data Intern | Leeds | <a href=\"https://acme.example/apply/2\">Apply</a> | Sep 29 |",
        "| Closed Co | Old Role \U0001F512 | Bristol | [Apply](https://x.example/3) | Jan 1 |",
        "",
        "| Position | Employer | Link |",
        "|---|---|---|",
        "| Analyst | Beta Ltd | [<img src=\"images/apply.png\" alt=\"Apply\">](https://beta.example/9) |",
    ])
    _rows = _gb._rows_from_pipe_tables(_md)
    check("github parser: badge images are never taken as the apply link",
          lambda: _rows[0]["url"] == "https://acme.example/apply/1" or f"got {_rows[0]['url']}")
    check("github parser: Markdown link text with its own brackets is kept whole",
          lambda: _gb._clean(_rows[0]["title_raw"]) == "[2027] Software Engineer, Early Career"
          or f"got {_rows[0]['title_raw']!r}")
    check("github parser: HTML links, closed rows and reordered columns all handled",
          lambda: (_rows[1]["url"] == "https://acme.example/apply/2" and _rows[2]["closed"]
                   and _rows[3]["company_raw"] == "Beta Ltd"
                   and _rows[3]["url"] == "https://beta.example/9")
          or f"rows were {_rows}")
    _seasons = [r for r, _, _ in _gb.expand_repos(_date(2027, 6, 1))]
    check("github repos: seasons roll over without a code change",
          lambda: "SimplifyJobs/Summer2027-Internships" in _seasons
          and "SimplifyJobs/Summer2028-Internships" in _seasons or f"got {_seasons[:4]}")

    # ---- the employer's own link always wins over an aggregator's copy
    _lk_db = ROOT / "out" / "_audit_links.db"
    if _lk_db.exists():
        _lk_db.unlink()
    _lk = Store(_lk_db)
    _direct = Job(source="ashby", source_kind="ats_direct", company="LinkCo",
                  title="Graduate Analyst", url="https://jobs.ashbyhq.com/linkco/1",
                  location="London", description="The full advert from the employer.")
    _copy = Job(source="arbeitnow", source_kind="aggregator", company="LinkCo",
                title="Graduate Analyst", url="https://www.arbeitnow.com/jobs/linkco-1",
                location="London", description="Short excerpt")
    _lk.upsert([_direct])
    _lk.upsert([_copy])
    _after_copy = _lk.get(_direct.id)
    check("links: an aggregator copy never replaces the employer's own link",
          lambda: (_after_copy["url"] == _direct.url
                   and _after_copy["description"].startswith("The full advert"))
          or f"link became {_after_copy['url']}")
    _first_seen_agg = Job(source="arbeitnow", source_kind="aggregator", company="LaterCo",
                          title="Placement Student", url="https://www.arbeitnow.com/jobs/lc",
                          location="Leeds")
    _later_direct = Job(source="greenhouse", source_kind="ats_direct", company="LaterCo",
                        title="Placement Student",
                        url="https://job-boards.greenhouse.io/laterco/jobs/9",
                        location="Leeds")
    _lk.upsert([_first_seen_agg])
    _lk.upsert([_later_direct])
    _upgraded = _lk.query(limit=10, search="Placement Student")[0]
    check("links: a job found later on the employer's own board switches to that link",
          lambda: (_upgraded["url"] == _later_direct.url
                   and _upgraded["source_kind"] == "ats_direct"
                   and _upgraded["source"] == "greenhouse")
          or f"still {_upgraded['source']} / {_upgraded['url']}")
    _lk.close()
    _lk_db.unlink()

    # ---- every scan: removed and expired postings
    _dl_db = ROOT / "out" / "_audit_delist.db"
    if _dl_db.exists():
        _dl_db.unlink()
    _dl = Store(_dl_db)
    _mk = lambda c, t, src="greenhouse", kind="ats_direct", **kw: Job(
        source=src, source_kind=kind, company=c, title=t, url=f"https://x.example/{c}/{t}", **kw)
    _gone = _mk("FullBoard", "Gone role")
    _kept = _mk("FullBoard", "Still there")
    _capped = _mk("BigBoard", "Past the cap")
    _agg = _mk("Agg Co", "Aggregator row", src="adzuna", kind="aggregator")
    _closed = _mk("Agg Co", "Closed role", src="adzuna", kind="aggregator",
                  closes_at="2020-01-01")
    _starred = _mk("FullBoard", "Starred gone role")
    _dl.upsert([_gone, _kept, _capped, _agg, _closed, _starred])
    _dl.conn.execute("UPDATE jobs SET last_seen = '2020-01-01T00:00:00+00:00'")
    _dl.conn.commit()
    _dl.toggle_star(_starred.id)
    _filtered_out = _mk("FullBoard", "Fetched but filtered out")
    _dl.upsert([_filtered_out])
    _dl.conn.execute("UPDATE jobs SET last_seen = '2020-01-01T00:00:00+00:00'")
    _dl.conn.commit()
    _dl.upsert([_kept])                                   # this "scan" saw only one row
    _res = _dl.mark_delisted("2026-01-01T00:00:00+00:00", {"FullBoard": "greenhouse"},
                             fetched_keys={_kept.dedupe_key, _filtered_out.dedupe_key})
    _vis = {j["title"] for j in _dl.query(limit=100)}
    check("every scan: a posting gone from a fully-read board is flagged and hidden",
          lambda: "Gone role" not in _vis and _dl.get(_gone.id)["delisted"]
          or "a taken-down posting is still showing")
    check("every scan: a posting fetched but filtered out is never judged taken down",
          lambda: not _dl.get(_filtered_out.id)["delisted"]
          or "a live posting that failed this scan's filters was hidden as removed")
    check("every scan: a board cut off at the cap is never judged",
          lambda: "Past the cap" in _vis or "a posting past the cap was wrongly delisted")
    check("every scan: aggregator rows are never judged by absence",
          lambda: "Aggregator row" in _vis or "an aggregator row was wrongly delisted")
    check("every scan: a posting past its closing date is flagged",
          lambda: "Closed role" not in _vis and _res["past_closing"] == 1
          or f"closing-date expiry missed ({_res})")
    check("every scan: a starred posting stays visible, flagged as no longer listed",
          lambda: "Starred gone role" in _vis and _dl.get(_starred.id)["delisted"]
          or "an engaged posting vanished or was not flagged")
    _dl.upsert([_gone])
    check("every scan: a posting that comes back is live again",
          lambda: not _dl.get(_gone.id)["delisted"] or "a re-listed posting stayed hidden")
    _dl.close()
    _dl_db.unlink()

    # ---- a board that fails part-way is never mistaken for a complete one
    from jobscout.sources import ats as _ats_mod
    _real_post = _ats_mod.SESSION.post_json

    def _workday_pages(pages):
        """Stub SESSION.post_json: hand back the given pages in turn (None = failure)."""
        it = iter(pages)
        return lambda *a, **k: next(it, None)

    _page20 = {"jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/{i}"}
                               for i in range(20)], "total": 1033}
    _page5 = {"jobPostings": [{"title": f"Last {i}", "externalPath": f"/last/{i}"}
                              for i in range(5)]}
    try:
        _ats_mod.SESSION.post_json = _workday_pages([_page20, None])
        _partial = _ats_mod.WorkdaySource().fetch("audit:wd3:Partial", "Partial Co")
        _partial_ok = _ats_mod.fetch_was_complete("workday", "audit:wd3:Partial")
        _ats_mod.SESSION.post_json = _workday_pages([_page20, _page5])
        _whole = _ats_mod.WorkdaySource().fetch("audit:wd3:Whole", "Whole Co")
        _whole_ok = _ats_mod.fetch_was_complete("workday", "audit:wd3:Whole")
    finally:
        _ats_mod.SESSION.post_json = _real_post
    check("every scan: a Workday board whose page failed part-way counts as incomplete",
          lambda: (len(_partial) == 20 and _partial_ok is False)
          or f"{len(_partial)} rows, complete={_partial_ok} - live postings would be "
             "judged taken down")
    check("every scan: a Workday board that reached its real end counts as complete",
          lambda: (len(_whole) == 25 and _whole_ok is True)
          or f"{len(_whole)} rows, complete={_whole_ok}")
    check("every scan: run() judges removals only for fully-read companies",
          lambda: "fully_read_companies(" in _inspect.getsource(_pl.run)
          and "fetch_was_complete" in _inspect.getsource(_pl.fully_read_companies)
          or "removals are judged without checking the boards were read in full")
    _saved_counts = dict(_pl.LAST_BOARD_COUNTS)
    _saved_max = _ats_mod.MAX_PER_COMPANY
    try:
        _ats_mod.MAX_PER_COMPANY = 400
        _two = [{"company": "TwoBoards", "ats": "workday", "slug": "two:wd3:Grad"},
                {"company": "TwoBoards", "ats": "workday", "slug": "two:wd3:Main"},
                {"company": "Mixed", "ats": "greenhouse", "slug": "mixed"},
                {"company": "Mixed", "ats": "workday", "slug": "mixed:wd3:Grad"},
                {"company": "EmptyGH", "ats": "greenhouse", "slug": "emptygh"}]
        _ats_mod._record_completeness("workday", "two:wd3:Grad", True)
        _ats_mod._record_completeness("workday", "two:wd3:Main", False)   # hit the cap
        _ats_mod._record_completeness("workday", "mixed:wd3:Grad", True)
        _pl.LAST_BOARD_COUNTS.clear()
        _pl.LAST_BOARD_COUNTS.update({("workday", "two:wd3:Grad"): 23,
                                      ("workday", "two:wd3:Main"): 400,
                                      ("greenhouse", "mixed"): 12,
                                      ("workday", "mixed:wd3:Grad"): 0,
                                      ("greenhouse", "emptygh"): 0})
        _full = _pl.fully_read_companies(_two)
    finally:
        _pl.LAST_BOARD_COUNTS.clear()
        _pl.LAST_BOARD_COUNTS.update(_saved_counts)
        _ats_mod.MAX_PER_COMPANY = _saved_max
    check("every scan: a complete small board never vouches for a capped big one",
          lambda: "TwoBoards" not in _full
          or "AstraZeneca regression: 84 live postings would be hidden again")
    check("every scan: an employer with every board read in full is judged, all sources",
          lambda: _full.get("Mixed") == ["greenhouse", "workday"] or f"got {_full}")
    check("every scan: an empty single-request board proves nothing (could be a failure)",
          lambda: "EmptyGH" not in _full or "an empty, possibly failed, board was trusted")

    # ---- every scan: the stored list is rescored and refreshed
    import inspect as _insp
    _run_src = _insp.getsource(_pl.run)
    check("every scan: run() rescores everything stored and flags removed postings",
          lambda: "rescore(" in _run_src and "mark_delisted(" in _run_src
          or "a scan no longer refreshes stored rows")
    _reg = ROOT / "out" / "_audit_registry.yaml"
    _reg.write_text("companies: []\n", "utf-8")
    _rc = Config()
    _calls_reg: list = []
    _rc.sources.registry_refresh_days = 14
    _fresh = _pl.refresh_registry_if_stale(_rc, path=_reg,
                                           runner=lambda: _calls_reg.append(1))
    _old = _time.time() - 30 * 86400
    _os.utime(_reg, (_old, _old))
    _stale = _pl.refresh_registry_if_stale(
        _rc, path=_reg, runner=lambda: _calls_reg.append(1) or type("R", (), {"returncode": 0})())
    _rc.sources.registry_refresh_days = 0
    _off = _pl.refresh_registry_if_stale(_rc, path=_reg, runner=lambda: _calls_reg.append(1))
    _reg.unlink()
    check("every scan: the company list is refreshed only once it is stale",
          lambda: (_fresh, _stale, _off, len(_calls_reg)) == ("fresh", "refreshed", "disabled", 1)
          or f"got {(_fresh, _stale, _off, len(_calls_reg))}")
    check("sources: the GitHub trackers get their own larger allowance",
          lambda: Config().sources.source_limits.get("github_internships", 0) >= 2000
          or "the general per-source cap would discard most tracker listings")

    # ---- stability cache expiry
    _sc_path = ROOT / "out" / "_audit_stab_cache.json"
    _lc = _stab._LevelCache(_sc_path)
    _lc.put("Fresh Co", {"level": "stable", "label": "Stable"})
    _lc.data["old co"] = {"level": "stable", "label": "Stable", "computed_at": "2020-01-01"}
    check("stability cache: a fresh rating is shown, a stale one is not",
          lambda: _lc.get("Fresh Co") and _lc.get("Old Co") is None or "cache expiry broken")
    if _sc_path.exists():
        _sc_path.unlink()

    # ---- Google Jobs via SerpApi
    from jobscout.sources import google_jobs as _gj
    check("google jobs: does nothing (and spends no searches) without SERPAPI_KEY",
          lambda: _os.environ.get("SERPAPI_KEY")
          or (_gj.GoogleJobsSource().available() is False
              and _gj.GoogleJobsSource().fetch(query="intern") == [])
          or "ran without a key")
    _prev_key = _os.environ.get("SERPAPI_KEY")
    _os.environ["SERPAPI_KEY"] = "audit-not-a-real-key"
    _gsrc = _gj.GoogleJobsSource()
    _gsrc._search = lambda params: {"jobs_results": [
        {"title": "Summer Analyst", "company_name": "Bank", "location": "London",
         "description": "<p>Ten weeks</p>",
         "detected_extensions": {"posted_at": "3 days ago", "schedule_type": "Internship"},
         "apply_options": [{"title": "Bank Careers", "link": "https://bank.example/apply"}]},
        {"title": "No link at all", "company_name": "Ghost"}]}
    try:
        _gjobs = _gsrc.fetch(query="summer internship", home=geo.get("GB"))
        _gempty = _gsrc.fetch(query="", home=geo.get("GB"))
    finally:
        if _prev_key is None:
            _os.environ.pop("SERPAPI_KEY", None)
        else:
            _os.environ["SERPAPI_KEY"] = _prev_key
    check("google jobs: results map to postings with the real apply link and a date",
          lambda: (len(_gjobs) == 1 and _gjobs[0].url == "https://bank.example/apply"
                   and bool(_gjobs[0].posted_at) and _gjobs[0].employment_kind == "internship")
          or f"got {[(j.title, j.url, j.posted_at) for j in _gjobs]}")
    check("google jobs: a result with no link is dropped, never given an invented one",
          lambda: all(j.title != "No link at all" for j in _gjobs) or "invented a link")
    check("google jobs: an empty query spends no search",
          lambda: _gempty == [] or "searched for nothing")

    # ---- harvested boards: apply links -> ATS boards, self-pruning
    from jobscout import harvest as _hv
    _bf = _hv.board_for
    check("harvest: workday link with or without a locale gives the board",
          lambda: _bf("https://lbg.wd3.myworkdayjobs.com/en-US/LBG_Careers/job/x")
                  == _bf("https://lbg.wd3.myworkdayjobs.com/en-us/LBG_Careers/job/x")
                  == ("workday", "lbg:wd3:LBG_Careers") or "locale taken as the board")
    check("harvest: a bare locale or Workday's own paths are not boards",
          lambda: _bf("https://lbg.wd3.myworkdayjobs.com/en-GB") is None
                  and _bf("https://lbg.wd3.myworkdayjobs.com/wday/cxs/x") is None
                  or "plumbing harvested as a board")
    check("harvest: myworkdaysite and Oracle links map to their slug formats",
          lambda: _bf("https://wd3.myworkdaysite.com/recruiting/mdlz/External/job/1")
                  == ("workday", "mdlz:wd3:External:myworkdaysite")
                  and _bf("https://x.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9")
                  == ("oracle", "x.fa.em2.oraclecloud.com/CX_1") or "slug format drifted")
    check("harvest: greenhouse embeds give the company, never 'embed'",
          lambda: _bf("https://boards.greenhouse.io/embed/job_board?for=acme") == ("greenhouse", "acme")
                  and _bf("https://boards.greenhouse.io/embed") is None or "harvested 'embed'")
    check("harvest: links without a trailing slash still count",
          lambda: _bf("https://jobs.lever.co/acme") == ("lever", "acme")
                  and _bf("https://apply.workable.com/acme") == ("workable", "acme") or "missed")
    check("harvest: SmartRecruiters (robots.txt-blocked API) and junk are skipped",
          lambda: _bf("https://jobs.smartrecruiters.com/Acme/1") is None
                  and _bf("javascript:alert(1)") is None and _bf("") is None or "kept")
    check("harvest: board words are trimmed from a scraped employer name",
          lambda: (_hv.clean_company("Organon Searchjobs"), _hv.clean_company("Careers"))
                  == ("Organon", "Careers") or "name not cleaned")
    _hv_path = ROOT / "out" / "_audit_harvest.yaml"
    _hv_path.parent.mkdir(exist_ok=True)
    _hv._write(_hv_path, [
        {"company": "Gone", "ats": "lever", "slug": "gone", "misses": 2},
        {"company": "Alive", "ats": "lever", "slug": "alive", "misses": 2},
        {"company": "Unread", "ats": "lever", "slug": "unread", "misses": 2}])
    _hv_jobs = [Job(source="targetjobs", source_kind="board", company="New Co", title="Placement",
                    url="https://targetjobs.co.uk/x",
                    raw={"apply_url": "https://jobs.lever.co/newco/123"}),
                Job(source="targetjobs", source_kind="board", company="Known", title="Placement",
                    url="https://jobs.lever.co/known/1")]
    try:
        _hv_out = _hv.update(_hv_jobs, {("lever", "known")},
                             {("lever", "gone"): 0, ("lever", "alive"): 7}, _hv_path)
        _hv_left = {e["slug"]: e for e in _hv.load_harvested(_hv_path)}
    finally:
        _hv_path.unlink(missing_ok=True)
    check("harvest: a board empty MISS_LIMIT scans running is dropped, a live one reset",
          lambda: ("gone" not in _hv_left and _hv_left["alive"]["misses"] == 0
                   and _hv_left["alive"]["jobs_seen"] == 7) or f"got {_hv_left}")
    check("harvest: a board not read this scan is not aged",
          lambda: _hv_left.get("unread", {}).get("misses") == 2 or "aged without a read")
    check("harvest: new boards are added, ones already in the registry are not",
          lambda: ("newco" in _hv_left and "known" not in _hv_left
                   and _hv_out == {"added": 1, "dropped": 1, "total": 3}) or f"got {_hv_out}")

    # ---- TARGETjobs
    from jobscout.sources import targetjobs as _tj
    check("targetjobs: tracker wrappers unwrap to the employer's link",
          lambda: _tj.unwrap_link("https://ad.doubleclick.net/ddm/clk/1;2;?https://acme.com/a%3Fb%3D1")
                  == "https://acme.com/a?b=1"
                  and _tj.unwrap_link("https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Facme.com")
                  == "https://acme.com"
                  and _tj.unwrap_link("https://ad.doubleclick.net/ddm/clk/1") == ""
                  and _tj.unwrap_link("mailto:x@y.z") == "" or "wrapper kept")
    _tj_job = _tj.TargetJobsSource()._job({
        "title": "Finance Placement", "organisation": {}, "sourceOrganisationName": "Acme",
        "path": "/x/1", "nid": 5, "location": "x" * 200,
        "salary": {"ranges": ["£25,000 to £30,000"]}, "opportunityStartDate": 1788220800,
        "applicationDeadline": "2026-12-01T00:00:00Z", "body": "<p>Hi</p>"}, "Placement")
    check("targetjobs: no organisation falls back to the source name; prose location dropped",
          lambda: (_tj_job and _tj_job.company == "Acme" and _tj_job.location == ""
                   and _tj_job.salary_raw.startswith("£25,000") and _tj_job.closes_at
                   and "Start date: September 2026" in _tj_job.description) or f"got {_tj_job}")
    check("targetjobs: a posting with no employer at all is dropped",
          lambda: _tj.TargetJobsSource()._job({"title": "T", "path": "/p"}, "Placement") is None
                  or "invented an employer")

    # ---- the polite HTTP layer: per-host throttle, Retry-After, circuit breaker
    from jobscout.http import PoliteSession as _PS
    class _Resp:
        def __init__(self, h): self.headers = h
    check("http: Retry-After seconds, HTTP dates and junk",
          lambda: (_PS._retry_after(_Resp({"Retry-After": "120"})) == 120.0
                   and _PS._retry_after(_Resp({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
                   and _PS._retry_after(_Resp({"Retry-After": "soon"})) is None
                   and _PS._retry_after(_Resp({})) is None) or "misread Retry-After")
    _ps = _PS(min_gap=1.0)
    for _ in range(_PS.FAIL_LIMIT - 1):
        _ps._failed("dead.example", "ConnectionError")
    _ps_before = _ps._breaker_open("dead.example")
    _ps._failed("dead.example", "ConnectionError")
    check("http: the breaker opens at FAIL_LIMIT failures, not before",
          lambda: (not _ps_before and _ps._breaker_open("dead.example")
                   and "dead.example" in _ps.health()["failing_hosts"]) or "breaker wrong")
    check("http: an open breaker stops requests without touching the network",
          lambda: _ps.request("GET", "https://dead.example/x", check_robots=False) is None
                  and _ps.stats["breaker_skips"] == 1 and _ps.stats["requests"] == 0
                  or f"stats {_ps.stats}")
    _ps._failed("slow.example", "HTTP 429", rest=5)
    _ps.reset_health()
    check("http: a new scan clears the breakers and counters",
          lambda: (not _ps._breaker_open("dead.example") and not _ps.health()["failing_hosts"]
                   and not any(_ps.stats.values())) or "state carried over")
    import time as _t
    _ps._throttle("https://a.example/1")
    _t0 = _t.time()
    _ps._throttle("https://b.example/1")
    _other = _t.time() - _t0
    _ps._throttle("https://a.example/2")
    _same = _t.time() - _t0
    check("http: one host's throttle never delays another; the same host waits min_gap",
          lambda: (_other < 0.2 and _same >= 0.7) or f"other {_other:.2f}s same {_same:.2f}s")

    _ps2 = _PS(min_gap=1.0)
    _g0 = _ps2._gap("api.ashbyhq.com")
    _ps2._slow_down("api.ashbyhq.com")
    _g1 = _ps2._gap("api.ashbyhq.com")
    for _ in range(10):
        _ps2._slow_down("api.ashbyhq.com")
    _g2 = _ps2._gap("api.ashbyhq.com")
    _ps2.reset_health()
    check("http: a 429 widens that host's gap (capped), a new scan restores it",
          lambda: (_g0 < 1.0 and _g1 >= 1.0 and _g2 == 5.0
                   and _ps2._gap("api.ashbyhq.com") == _g0
                   and _ps2._gap("other.example") == 1.0) or f"gaps {_g0} {_g1} {_g2}")

    # ---- source health: what went quiet since the last scan
    from jobscout.pipeline import source_health as _sh
    _shr = _sh({"targetjobs": 0, "gradcracker": 40, "reed": 0},
               {"Acme": 0, "Beta": 12},
               {"per_source": {"targetjobs": 1200, "gradcracker": 35, "reed": 0},
                "per_company": {"Acme": 30, "Beta": 10, "Tiny": 2}})
    check("source health: a source or board that fell to zero is flagged, nothing else",
          lambda: ([q["source"] for q in _shr["sources_gone_quiet"]] == ["targetjobs"]
                   and [b["company"] for b in _shr["boards_gone_quiet"]] == ["Acme"])
                  or f"got {_shr}")
    check("source health: no previous scan flags nothing",
          lambda: _sh({"a": 0}, {"b": 0}, None)["sources_gone_quiet"] == [] or "flagged")

    # ---- quick add: a link or a screenshot fills the tracker's Add-a-job form
    from jobscout import capture as _cap, autofill as _af
    _png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    check("quick add: images are told apart by their bytes, not their name",
          lambda: ([(_cap.image_type(b) or ("", ""))[1] for b in (
              _png, b"\xff\xd8\xff\xe0xx", b"GIF89a..", b"RIFF\0\0\0\0WEBPVP8 ", b"<svg>")]
                   == ["png", "jpg", "gif", "webp", ""]) or "wrong type")
    _cfu = {
        "https://boards.greenhouse.io/monzo/jobs/123": "Monzo",
        "https://careers.rolls-royce.com/united-kingdom/job/123": "Rolls Royce",
        "https://jobs.bae-systems.com/role/1": "BAE Systems",
        "https://www.reed.co.uk/jobs/placement/123": "",
        "https://www.adzuna.co.uk/jobs/details/1": "",
    }
    check("quick add: the employer comes from the link, never a job board's name",
          lambda: {u: _cap.company_from_url(u) for u in _cfu} == _cfu
                  or f"got {({u: _cap.company_from_url(u) for u in _cfu})}")
    check("quick add: the employer is read from a posting's JobPosting data",
          lambda: _af._from_job_posting({"title": "Intern",
                                         "hiringOrganization": {"name": "Arup"}})["company"]
                  == "Arup" or "no company")

    def _capture_flow() -> object:
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile
        import app as _app
        _tmp = Path(_tempfile.mkdtemp())
        saved_store, saved_dir = _app.store, _cap.SHOTS_DIR
        saved_key = _os.environ.pop("ANTHROPIC_API_KEY", None)
        _app.store, _cap.SHOTS_DIR = Store(_tmp / "jobs.db"), _tmp / "shots"
        try:
            c = _app.app.test_client()
            hdr = {"X-Job-Scout": "1"}
            form = lambda: {"company": "Quickco", "title": "Placement", "start": "September 2027",
                            "duration": "12 months", "salary": "£24,000",
                            "screenshot": (__import__("io").BytesIO(_png), "s.png")}
            if c.post("/api/tracker/add", data=form()).status_code != 403:
                return "a screenshot upload without the page's header was accepted"
            r = c.post("/api/tracker/add", data=form(), headers=hdr)
            if r.status_code != 200 or not r.json.get("created"):
                return f"add with a screenshot failed: {r.status_code} {r.json}"
            jid = r.json["id"]
            row = next(j for j in c.get("/api/tracker").json["jobs"] if j["id"] == jid)
            if not row["screenshot"]:
                return "the tracker does not report the screenshot"
            if row["tracker_extra"].get("Start Date") != "September 2027" or \
                    row["tracker_extra"].get("Duration") != "12 months":
                return f"start/duration not kept: {row['tracker_extra']}"
            if row["salary_display"] != "£24,000":
                return f"salary not kept: {row['salary_display']}"
            g = c.get(f"/api/job/{jid}/screenshot")
            if g.status_code != 200 or g.mimetype != "image/png" or g.data != _png:
                return f"screenshot not served back: {g.status_code} {g.mimetype}"
            g.close()                     # Windows will not delete an open file
            if c.get("/api/job/..%2F..%2Fapp/screenshot").status_code != 404:
                return "a path in the job id was not refused"
            bad = c.post("/api/tracker/add", headers=hdr, data={
                "company": "X", "screenshot": (__import__("io").BytesIO(b"<svg/>"), "x.png")})
            if bad.status_code != 400:
                return "a non-image screenshot was accepted"
            if c.post("/api/tracker/add", json={"company": "Plainco"}).status_code != 200:
                return "typing a job in by hand (JSON) stopped working"
            rd = c.post("/api/tracker/read", headers=hdr,
                        data={"image": (__import__("io").BytesIO(_png), "s.png")}).json
            if rd["fields"] or "ANTHROPIC_API_KEY" not in rd["message"]:
                return f"no-key screenshot read should explain itself: {rd}"
            if c.post("/api/tracker/read", json={"url": "not a link"}).json["fields"]:
                return "a non-link produced fields"
            u = c.post(f"/api/job/{jid}/untrack")
            if _cap.shot_path(jid) is not None:
                return f"removing a job you added left its screenshot behind ({u.status_code})"
            return True
        finally:
            _app.store.close()
            _app.store, _cap.SHOTS_DIR = saved_store, saved_dir
            if saved_key is not None:
                _os.environ["ANTHROPIC_API_KEY"] = saved_key
            _shutil.rmtree(_tmp, ignore_errors=True)

    check("quick add: screenshot + typed details save, serve back and delete with the job",
          _capture_flow)

    # ---- the page itself
    _page = (ROOT / "templates" / "index.html").read_text("utf-8")
    _ids = _sec_re.findall(r'\sid="([^"]+)"', _page)
    check("page: no element id appears twice",
          lambda: len(_ids) == len(set(_ids))
          or f"duplicated: {sorted({i for i in _ids if _ids.count(i) > 1})}")

    check("saved filters: an old config with no view section loads with defaults",
          lambda: _Cfg().view == _ViewPrefs() or "defaults drifted")
    check("viewed: opening a job's detail panel is what records the view",
          lambda: "store.mark_viewed(job_id)" in _inspect.getsource(_security_app.api_job)
          or "api_job no longer marks the posting as viewed")

    # =====================================================================
    #  NEW JOB SOURCES - Gradcracker/RateMyPlacement/Bright Network, Reddit,
    #  GitHub internship trackers, USAJobs, custom feeds (see README "Adding
    #  coverage" / "The community/forum/student-board tier")
    # =====================================================================
    from jobscout.sources import ALL_ADAPTERS, COMMUNITY_ADAPTERS
    from jobscout.sources.reddit import RedditSource
    from jobscout.sources.customfeeds import _looks_like_feed, _parse_feed
    from jobscout.sources.github_boards import GitHubInternshipsSource

    _NEW_NAMES = ("gradcracker", "ratemyplacement", "brightnetwork",
                  "github_internships", "reddit", "usajobs", "custom_feeds")
    check("new sources: all seven registered in ALL_ADAPTERS",
          lambda: all(n in ALL_ADAPTERS for n in _NEW_NAMES)
          or f"missing: {[n for n in _NEW_NAMES if n not in ALL_ADAPTERS]}")
    check("new sources: all seven also in COMMUNITY_ADAPTERS",
          lambda: all(n in COMMUNITY_ADAPTERS for n in _NEW_NAMES)
          or "ALL_ADAPTERS and COMMUNITY_ADAPTERS have drifted apart")

    _gb, _us, _au = geo.get("GB"), geo.get("US"), geo.get("AU")
    check("gradcracker/ratemyplacement/brightnetwork are GB-only",
          lambda: all(ALL_ADAPTERS[n].markets == ("GB",)
                      for n in ("gradcracker", "ratemyplacement", "brightnetwork"))
          or "a UK student board is not correctly scoped to GB")
    check("gradcracker serves GB but not US or AU",
          lambda: (ALL_ADAPTERS["gradcracker"].serves(_gb)
                   and not ALL_ADAPTERS["gradcracker"].serves(_us)
                   and not ALL_ADAPTERS["gradcracker"].serves(_au))
          or "GB-only source leaking into other markets, or wrongly excluding GB")
    check("usajobs is US-only and skips GB/AU",
          lambda: (ALL_ADAPTERS["usajobs"].markets == ("US",)
                   and ALL_ADAPTERS["usajobs"].serves(_us)
                   and not ALL_ADAPTERS["usajobs"].serves(_gb)
                   and not ALL_ADAPTERS["usajobs"].serves(_au))
          or "USAJobs not correctly scoped to the US")
    check("reddit/github_internships/custom_feeds serve every market",
          lambda: all(ALL_ADAPTERS[n].serves(c)
                      for n in ("reddit", "github_internships", "custom_feeds")
                      for c in (_gb, _us, _au))
          or "a global source is wrongly scoped to one market")

    check("reddit.available() is False with no credentials",
          lambda: ALL_ADAPTERS["reddit"].available() is False
          or "reports available with no REDDIT_CLIENT_ID/SECRET set in this environment")
    check("reddit.fetch() degrades to [] with no credentials, never raises",
          lambda: ALL_ADAPTERS["reddit"].fetch(max_results=5) == []
          or "did not degrade cleanly")
    check("usajobs.available() is False with no credentials",
          lambda: ALL_ADAPTERS["usajobs"].available() is False
          or "reports available with no USAJOBS_API_KEY/USER_AGENT set")
    check("usajobs.fetch() degrades to [] with no credentials, never raises",
          lambda: ALL_ADAPTERS["usajobs"].fetch(max_results=5) == []
          or "did not degrade cleanly")
    check("brightnetwork.fetch() always returns [] (Cloudflare JS challenge, no bypass)",
          lambda: ALL_ADAPTERS["brightnetwork"].fetch(query="engineering", max_results=50) == []
          or "started returning data - the Cloudflare-block docstring is now stale")
    check("custom_feeds.fetch() with no feed_urls returns [] (opt-in, not a bug)",
          lambda: ALL_ADAPTERS["custom_feeds"].fetch() == []
          or "fetched something with no configured URL")

    check("reddit fallback: bracket-tagged title kept, company never guessed",
          lambda: (lambda r: r["is_posting"] and r["company"] == "See posting")(
              RedditSource()._fallback_extract(
                  "[Hiring] Summer 2027 SWE Intern at a fintech", {}))
          or "fallback either dropped a clear posting or invented a company name")
    check("reddit fallback: bare 'internship' mention alone is not treated as a posting",
          lambda: RedditSource()._fallback_extract(
              "Anyone else still waiting to hear back about their internship?", {}
          )["is_posting"] is False
          or "regression: the tight fallback regex is back to bare-matching 'internship'")
    check("reddit fallback: explicit hiring phrase is kept",
          lambda: RedditSource()._fallback_extract(
              "We're hiring a data science intern for spring", {})["is_posting"] is True
          or "an explicit hiring phrase was dropped")

    _rss_fixture = """<?xml version="1.0"?><rss><channel>
      <item><title>Graduate Analyst</title><link>https://example.com/jobs/1</link>
      <description>A real posting</description></item>
    </channel></rss>"""
    check("customfeeds: recognises a real RSS document",
          lambda: _looks_like_feed(_rss_fixture) or "did not detect RSS")
    check("customfeeds: parses title/link/description out of RSS",
          lambda: (lambda entries: len(entries) == 1
                   and entries[0]["title"] == "Graduate Analyst"
                   and entries[0]["url"] == "https://example.com/jobs/1")(
              _parse_feed(_rss_fixture, "https://example.com/feed"))
          or "RSS parsing drifted")
    check("customfeeds: malformed XML degrades to [], never raises",
          lambda: _parse_feed("<rss><channel><item><title>oops", "https://x.test") == []
          or "malformed feed was not handled gracefully")
    check("customfeeds: plain HTML is not misdetected as a feed",
          lambda: _looks_like_feed("<!DOCTYPE html><html><body>hi</body></html>") is False
          or "an ordinary HTML page was treated as RSS/Atom")

    check("github_internships: a dead/renamed repo never blocks the others",
          lambda: "Ouckah/Summer2026-Internships" in GitHubInternshipsSource.repos
          or "the known-gone repo was removed from the list - the try/except-per-repo "
             "path this check exercises is no longer being tested for real")
    _gh_live = ALL_ADAPTERS["github_internships"].fetch(max_results=25)
    check("github_internships: a live fetch returns real postings",
          lambda: len(_gh_live) > 0 or "no jobs came back - network issue, or both live "
                                       "repos changed format at once")
    check("github_internships: every returned job has a real company/title/url",
          lambda: all(j.company and j.title and j.url.startswith(("http://", "https://"))
                      for j in _gh_live)
          or "a row with a missing field or an invented URL slipped through")

    for temp in (fixture, audit_db):
        if temp.exists():
            temp.unlink()
    import shutil as _cleanup_shutil
    _cleanup_shutil.rmtree(audit_accounts_dir, ignore_errors=True)

    check("audit hygiene: the real job scores/flags, account folders and config.yaml "
          "are exactly as they were before the audit ran",
          lambda: _real_state() == _real_before
          or "the audit changed the user's real data (if the app was being used "
             "during the run, re-run it with the app idle to confirm)")

    # ---------------------------------------------------------------- report

    print(f"ran {CHECKS} checks")
    if FAILURES:
        print(f"\n{len(FAILURES)} FINDING(S):\n")
        for f in FAILURES:
            print("  -", f)
    else:
        print("no findings")

    cleanup = ROOT / "out" / "_audit_empty.xlsx"
    if cleanup.exists():
        cleanup.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
