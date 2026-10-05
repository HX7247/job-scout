"""Job Scout web app - local Flask server.

Run:  python app.py     then open http://127.0.0.1:5000
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, jsonify, render_template, request, send_from_directory,
                   session)

from jobscout import apply as apply_pack_builder
from jobscout import (accounts, assistant, autofill, capture, classify, enrich, geo, interview,
                      linkedin_import, privacy, review, rules as rules_mod, scoring,
                      sponsorship, stability, tracker, tracker_import, why as why_mod)
from jobscout.config import Config
from jobscout.linkedin import company_slug, outreach_pack
from jobscout.models import Job
from jobscout.pipeline import load_profile, rescore, run as run_pipeline
from jobscout.store import APP_STATUSES, STATUSES, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobscout.app")

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
# Signed cookies only, and the key lives in data/ so signing in survives a restart.
app.secret_key = accounts.session_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

store = Store()
store.apply_startup_flags(stability.startup_flags())   # registry may have new tags
ACCOUNTS = accounts.Accounts()


# ------------------------------------------------------------------ security
# Headers cost nothing and this app has no inline <script> or event-handler
# attributes anywhere (checked - everything is addEventListener in the three
# external files below), so a strict script-src is free hardening, not a
# refactor. style-src stays permissive because inline style="..." attributes
# are used throughout the generated HTML and rewriting all of them is a much
# bigger change than a security pass justifies on its own.
_CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' data: https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def _security_headers(response):
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=()")
    # No HSTS: this app is served over plain http://127.0.0.1 by design (see
    # app.run at the bottom), and HSTS on a non-HTTPS origin does nothing but
    # would actively break it if a future change ever did add TLS without also
    # updating this - better to add it deliberately then, not carry a header
    # that lies about the current transport.
    return response


# Password-verifying endpoints (login, change password, delete account) share one
# rate limiter, keyed by username rather than IP - every request here arrives from
# 127.0.0.1, so an IP bucket would either do nothing or amount to the same thing as
# a username bucket. The real threat for a local app is a script guessing faster
# than a person could, not a remote attacker rotating addresses; scrypt's own cost
# (see accounts.py's SCRYPT params) already taxes each guess, this just caps how
# many guesses get that far.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 8


def _rate_key(username: str) -> str:
    return (username or "").strip().lower()


def _login_rate_limited(username: str) -> bool:
    key = _rate_key(username)
    now = time.monotonic()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        _LOGIN_ATTEMPTS[key] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(username: str) -> None:
    key = _rate_key(username)
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())


def _clear_login_failures(username: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(_rate_key(username), None)

# A profile imported before signing in. Deliberately in memory and not in the session
# cookie: a cookie is written to the browser's disk, which is exactly where a profile
# should not end up when the user has not asked for it to be kept. Dies with the process,
# which is what "held for this session only" should mean.
PENDING: dict[str, dict] = {}


def pending_profile() -> dict | None:
    token = session.get("pending_token")
    return PENDING.get(token) if token else None


def hold_pending(profile: dict) -> None:
    import secrets
    token = session.get("pending_token") or secrets.token_hex(8)
    session["pending_token"] = token
    PENDING[token] = profile
    # One unsaved profile per process is plenty; this is a single-user local app.
    for old in list(PENDING)[:-8]:
        PENDING.pop(old, None)


def drop_pending() -> None:
    token = session.pop("pending_token", None)
    if token:
        PENDING.pop(token, None)

# Background scrape state, read by the UI to drive the progress bar.
SCRAPE = {"running": False, "stage": "", "done": 0, "total": 0, "found": 0,
          "message": "", "finished_at": None, "error": None, "summary": None}
_scrape_lock = threading.Lock()


# --------------------------------------------------------------------- identity
# Signing in is optional and protects your PROFILE, not the job list. The job list is
# employers' public adverts and holds nothing about you, so gating it would be theatre.
# Your derived profile - degree, skills, region - loads only once you are signed in.
def current_account() -> dict | None:
    account_id = session.get("account_id")
    return ACCOUNTS.get(account_id) if account_id else None


def cfg() -> Config:
    """Reload each request so edits made outside the app are picked up.

    Signed in, this is the account's own criteria; signed out, the shared config.yaml.

    An account with no config file of its own gets blank defaults, never the shared
    file - falling back to it used to mean a second or later account on a shared
    computer would transparently read (and, on their first save, start from) whatever
    titles, locations and keywords an earlier signed-out session had left behind, with
    neither person ever asking for that. The one legitimate case, a brand-new install's
    first account picking up criteria tuned signed-out minutes earlier, is handled once
    and explicitly in api_account_create by writing that account's file immediately, so
    this function never needs to guess whether falling back is appropriate.
    """
    account = current_account()
    if account:
        path = ACCOUNTS.config_path(account["id"])
        return Config.load(path) if path.exists() else Config()
    return Config.load()


def save_cfg(configuration: Config) -> None:
    account = current_account()
    configuration.save(ACCOUNTS.config_path(account["id"]) if account else None)


def derived_profile() -> dict | None:
    """The profile built from the user's LinkedIn export, if they have imported one."""
    account = current_account()
    if account:
        return ACCOUNTS.load_profile(account["id"]) or pending_profile()
    return pending_profile()


def matching_profile():
    """The CVProfile used for scoring, explanations and interview briefs."""
    try:
        return load_profile(cfg(), derived_profile())
    except Exception as exc:
        log.warning("could not build a matching profile: %s", exc)
        return None


def active_rules(selected: list[str] | None = None) -> list:
    """The facet rules the user has actually ticked, for narrowing a query.

    Only ticked facets, and never the exclude/require rules. Those are hard filters and
    are applied once during scoring; passing them here would turn an "exclude nights"
    rule into "show me only night work" and empty the table, which is exactly what an
    earlier version of this function did.
    """
    if not selected:
        return []
    wanted = set(selected)
    return [r for r in cfg().rules() if r.facet and r.key in wanted]


# ----------------------------------------------------------------------- page
@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------------------------------------------------- data
def _num(value, default, low=None, high=None):
    """Query strings come from the browser; never let a bad one 500 the endpoint."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


@app.route("/api/jobs")
def api_jobs():
    args = request.args
    filters = dict(
        status=args.get("status", "all"),
        source=args.get("source", "all"),
        company=args.get("company", "").strip() or None,
        min_score=_num(args.get("min_score"), 0.0, 0, 100),
        search=args.get("q", "").strip(),
        starred_only=args.get("starred") == "1",
        remote_only=args.get("remote") == "1",
        sponsored_only=args.get("sponsored") == "1",
        salary_disclosed_only=args.get("salary_disclosed") == "1",
        unviewed_only=args.get("unviewed") == "1",
        startups=(args.get("startups") if args.get("startups") in ("hide", "only")
                  else "any"),
        employment=[v for v in args.getlist("employment") if v],
        families=[v for v in args.getlist("family") if v],
        include_unclassified=args.get("unclassified", "1") == "1",
        own_rules=active_rules([v for v in args.getlist("rule") if v]),
    )
    jobs = store.query(**filters, order=args.get("order", "score"),
                       limit=int(_num(args.get("limit"), 200, 1, 2000)),
                       offset=int(_num(args.get("offset"), 0, 0, 500000)))
    for job in jobs:
        job["description"] = (job.get("description") or "")[:400]
        # Only what a detail panel already worked out - never a network call per row.
        cached = stability.LEVEL_CACHE.get(job["company"])
        job["stability"] = cached["level"] if cached else ""
        job["stability_label"] = cached["label"] if cached else ""
    # "total" is every match, not just this page; "facets" are the sidebar numbers
    # for the same filters, so ticking one box updates the counts beside the others.
    return jsonify({
        "jobs": jobs, "count": len(jobs), "total": store.count(**filters),
        "facets": store.facet_counts(
            **filters, all_rules=[r for r in cfg().rules() if r.facet]),
    })


@app.route("/api/job/<job_id>")
def api_job(job_id: str):
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    store.mark_viewed(job_id)

    configuration = cfg()
    profile = matching_profile()

    job = Job(
        source=row["source"], source_kind=row["source_kind"], company=row["company"],
        title=row["title"], url=row["url"], location=row["location"] or "",
        description=row["description"] or "",
    )
    job.matched_skills = row.get("matched_skills") or []

    sponsor = None
    if row.get("sponsor_name"):
        sponsor = {"name": row["sponsor_name"], "city": ""}
    try:
        # The apply URL lets enrich confirm the Wikidata entity is really this
        # employer rather than a namesake - it was not being passed from here.
        company = enrich.company_profile(row["company"], sponsor, job_url=row["url"])
    except Exception as exc:                 # never let enrichment break the panel
        log.warning("enrichment failed for %s: %s", row["company"], exc)
        company = None

    try:
        rating = stability.assess(
            row["company"], profile=company,
            sponsor_rating=row.get("sponsor_rating") or "",
            sponsor_name=row.get("sponsor_name") or "",
            sponsor_confidence=float(row.get("sponsor_conf") or 0), job_url=row["url"],
            activity=store.company_activity(row["company"]))
        stability.LEVEL_CACHE.put(row["company"], rating)
    except Exception as exc:
        log.warning("stability rating failed for %s: %s", row["company"], exc)
        rating = None

    return jsonify({
        "job": row,
        "company": company,
        "company_confirmed": stability.wikidata_confirmed(row["company"], company, row["url"]),
        "stability": rating,
        "outreach": outreach_pack(job, profile, configuration.profile.school,
                                  configuration.profile.school_linkedin_slug,
                                  home.name if (home := configuration.home_country()) else "",
                                  configuration.search.career_stage or "student"),
    })


def _job_from_row(row: dict) -> Job:
    job = Job(
        source=row["source"], source_kind=row["source_kind"], company=row["company"],
        title=row["title"], url=row["url"], location=row["location"] or "",
        remote=bool(row["remote"]), description=row["description"] or "",
        employment_type=row["employment_type"] or "", department=row["department"] or "",
        salary_min=row["salary_min"], salary_max=row["salary_max"],
        salary_currency=row["salary_currency"] or "",
        posted_at=row["posted_at"], closes_at=row["closes_at"],
    )
    job.matched_skills = row.get("matched_skills") or []
    job.missing_skills = row.get("missing_skills") or []
    job.score = row.get("score") or 0
    job.score_reasons = row.get("score_reasons") or []
    return job


@app.route("/api/deck")
def api_deck():
    """Cards still awaiting a decision, best first."""
    limit = int(request.args.get("limit", 25))
    jobs = store.query(
        status="new", min_score=_num(request.args.get("min_score"), 0.0, 0, 100),
        order="score", limit=limit,
        employment=[v for v in request.args.getlist("employment") if v],
        families=[v for v in request.args.getlist("family") if v],
        include_unclassified=request.args.get("unclassified", "1") == "1",
        own_rules=active_rules([v for v in request.args.getlist("rule") if v]))
    for job in jobs:
        job["description"] = (job.get("description") or "")[:700]
    stats = store.stats()
    reviewed = sum(count for status, count in stats["by_status"].items()
                   if status != "new")
    return jsonify({"cards": jobs, "remaining": len(jobs),
                    "reviewed": reviewed,
                    "shortlisted": stats["by_status"].get("shortlisted", 0)})


# What each swipe means. Nothing here submits anything to an employer.
DECISIONS = {"shortlist": "shortlisted", "pass": "dismissed",
             "star": "shortlisted", "reset": "new"}


@app.route("/api/job/<job_id>/decide", methods=["POST"])
def api_decide(job_id: str):
    decision = (request.json or {}).get("decision", "")
    if decision not in DECISIONS:
        return jsonify({"error": f"decision must be one of {sorted(DECISIONS)}"}), 400
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404

    previous = row["status"]
    store.set_status(job_id, DECISIONS[decision])
    if decision == "star" and not row["starred"]:
        store.toggle_star(job_id)
    if decision == "reset" and row["starred"]:
        store.toggle_star(job_id)

    return jsonify({"ok": True, "status": DECISIONS[decision], "previous": previous})


@app.route("/api/job/<job_id>/pack", methods=["GET"])
def api_pack(job_id: str):
    """Everything needed to submit this application except the click."""
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404

    configuration = cfg()
    profile = matching_profile()

    sponsor = None
    if row.get("sponsor_name"):
        sponsor = {"name": row["sponsor_name"], "ambiguous": False,
                   "rating_letter": row.get("sponsor_rating", "")}
    try:
        company = enrich.company_profile(row["company"], sponsor, job_url=row["url"])
    except Exception:
        company = None

    job = _job_from_row(row)
    # One job in the hand: worth the extra lookup to name the sponsor precisely.
    precise = scoring.sponsor_info(job, configuration, deep=True)
    if precise:
        sponsor = precise

    pack = apply_pack_builder.build_pack(
        job, profile, configuration, company, sponsor)
    return jsonify(pack.to_dict())


@app.route("/api/job/<job_id>/review", methods=["GET", "POST"])
def api_review(job_id: str):
    """How well a CV and cover letter fit this one job - see jobscout/review.py.

    A cover letter can be supplied in the POST body (whatever the user already has in
    the draft textarea); with none supplied, one is generated the same way the apply
    pack's letter is, so the review has something to say about wording either way. Pass
    {"skip_letter": true} to review the CV alone.
    """
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404

    configuration = cfg()
    profile = matching_profile()
    # get_json(silent=True), not request.json: this route also accepts a plain GET
    # (no body, no Content-Type at all, exactly what the browser sends for a first
    # look), and request.json raises a 415 on that rather than returning None.
    payload = request.get_json(silent=True) or {}

    try:
        company = enrich.company_profile(row["company"], job_url=row["url"])
    except Exception:
        company = None

    job = _job_from_row(row)
    cover_letter = (payload.get("cover_letter") or "").strip()
    if not cover_letter and not payload.get("skip_letter"):
        try:
            cover_letter = apply_pack_builder.build_pack(
                job, profile, configuration, company).cover_letter
        except Exception as exc:
            log.warning("could not draft a letter for the review: %s", exc)

    result = review.build_review(
        job, profile, configuration, company, cover_letter,
        use_model=request.args.get("model", "1") == "1")
    return jsonify(result.to_dict())


@app.route("/api/job/<job_id>/status", methods=["POST"])
def api_status(job_id: str):
    status = (request.json or {}).get("status", "")
    if not store.set_status(job_id, status):
        return jsonify({"error": f"status must be one of {STATUSES}"}), 400
    return jsonify({"ok": True, "status": status})


@app.route("/api/job/<job_id>/star", methods=["POST"])
def api_star(job_id: str):
    return jsonify({"ok": True, "starred": bool(store.toggle_star(job_id))})


@app.route("/api/job/<job_id>/note", methods=["POST"])
def api_note(job_id: str):
    store.set_note(job_id, (request.json or {}).get("note", ""))
    return jsonify({"ok": True})


# ------------------------------------------------------------------ tracker
_TRACKER_FIELDS = ("id", "company", "title", "url", "location", "closes_at", "status",
                   "app_status", "applied_at", "notes", "source", "source_kind",
                   "salary_display", "delisted", "tracker_extra", "status_changed_at")


@app.route("/api/tracker")
def api_tracker():
    rows = [{**{k: job.get(k) for k in _TRACKER_FIELDS},
             "screenshot": capture.shot_path(job["id"]) is not None}
            for job in store.tracked()]
    return jsonify({"jobs": rows, "statuses": APP_STATUSES})


@app.route("/api/tracker/import", methods=["POST"])
def api_tracker_import():
    if not _upload_allowed():
        return jsonify({"error": "Upload from the Job Scout page."}), 403
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Choose a spreadsheet to upload."}), 400
    data = upload.read(tracker_import.MAX_BYTES + 1)
    try:
        result = tracker_import.import_tracker(store, data, upload.filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.warning("tracker import failed: %s", exc)
        return jsonify({"error": f"Could not read that file: {exc}"}), 400
    return jsonify(result)


def _upload_allowed() -> bool:
    # A file upload is a "simple" request any web page could send to localhost
    # without a CORS preflight; a custom header can only come from this page.
    return request.headers.get("X-Job-Scout") == "1"


@app.route("/api/tracker/read", methods=["POST"])
def api_tracker_read():
    """Fill the Add-a-job form from a link or a screenshot. Saves nothing."""
    image = request.files.get("image")
    if image is not None:
        if not _upload_allowed():
            return jsonify({"error": "Upload from the Job Scout page."}), 403
        data = image.read(capture.MAX_SHOT_BYTES + 1)
        if len(data) > capture.MAX_SHOT_BYTES:
            return jsonify({"error": "That image is over 8 MB."}), 400
        fields, message = capture.read_screenshot(data)
    else:
        fields, message = capture.read_link((request.json or {}).get("url", ""))
    return jsonify({"fields": fields, "message": message})


@app.route("/api/tracker/add", methods=["POST"])
def api_tracker_add():
    """A job found somewhere else - a careers fair, a friend, a site we do not scan.
    JSON, or a form with a screenshot of the posting to keep with it."""
    shot = request.files.get("screenshot")
    if request.files and not _upload_allowed():
        return jsonify({"error": "Upload from the Job Scout page."}), 403
    payload = request.form if request.files or request.form else (request.json or {})
    shot_data = b""
    if shot is not None and shot.filename:
        shot_data = shot.read(capture.MAX_SHOT_BYTES + 1)
        if len(shot_data) > capture.MAX_SHOT_BYTES:
            return jsonify({"error": "That image is over 8 MB."}), 400
        if capture.image_type(shot_data) is None:
            return jsonify({"error": "That is not a PNG, JPEG, GIF or WebP image."}), 400
    company = (payload.get("company") or "").strip()
    if not company:
        return jsonify({"error": "Company is needed."}), 400
    stage = payload.get("app_status") or "Not applied yet"
    if stage not in APP_STATUSES:
        return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
    url = (payload.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    job_id, created = tracker_import.add_manual(
        store, company=company, title=payload.get("title") or "", url=url,
        location=payload.get("location") or "",
        deadline=tracker_import.parse_day(payload.get("deadline")),
        salary=(payload.get("salary") or "").strip())
    store.set_app_status(job_id, stage)
    note = (payload.get("notes") or "").strip()
    if note:
        store.set_note(job_id, note)
    extra = dict((store.get(job_id) or {}).get("tracker_extra") or {})
    for key, column in (("start", "Start Date"), ("duration", "Duration")):
        value = (payload.get(key) or "").strip()
        if value:
            extra[column] = value
    if extra:
        store.set_tracker_extra(job_id, extra)
    if shot_data:
        capture.save_shot(job_id, shot_data)
    return jsonify({"ok": True, "id": job_id, "created": created})


@app.route("/api/job/<job_id>/screenshot", methods=["GET", "POST"])
def api_job_screenshot(job_id: str):
    """The screenshot kept with a tracked job: view it, or attach / replace it."""
    if request.method == "GET":
        path = capture.shot_path(job_id)
        if path is None:
            return jsonify({"error": "no screenshot"}), 404
        return send_from_directory(path.parent, path.name, max_age=0)
    if not _upload_allowed():
        return jsonify({"error": "Upload from the Job Scout page."}), 403
    if store.get(job_id) is None:
        return jsonify({"error": "not found"}), 404
    image = request.files.get("image")
    if image is None:
        return jsonify({"error": "Choose an image."}), 400
    problem = capture.save_shot(job_id, image.read(capture.MAX_SHOT_BYTES + 1))
    if problem:
        return jsonify({"error": problem}), 400
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/track", methods=["POST"])
def api_track(job_id: str):
    """Put a job on the tracker, or change its stage / date applied."""
    payload = request.json or {}
    if store.get(job_id) is None:
        return jsonify({"error": "not found"}), 404
    if "app_status" in payload or not (store.get(job_id) or {}).get("tracked"):
        stage = payload.get("app_status") or "Not applied yet"
        if not store.set_app_status(job_id, stage):
            return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
    if "applied_at" in payload:
        store.set_applied_at(job_id, tracker_import.parse_day(payload["applied_at"]))
    job = store.get(job_id)
    return jsonify({"ok": True, **{k: job.get(k) for k in _TRACKER_FIELDS}})


@app.route("/api/job/<job_id>/autofill", methods=["POST"])
def api_autofill(job_id: str):
    """Read the job's own posting and fill whatever is still blank on it."""
    try:
        result = autofill.autofill(store, job_id)
    except KeyError:
        return jsonify({"error": "not found"}), 404
    job = store.get(job_id)
    return jsonify({"ok": True, **result, "job": {k: job.get(k) for k in _TRACKER_FIELDS}})


@app.route("/api/job/<job_id>/untrack", methods=["POST"])
def api_untrack(job_id: str):
    """Take a job off the tracker. One that only exists because it was typed in or
    imported is removed; a scanned posting goes back to the jobs list as 'new'."""
    if store.delete_manual(job_id):
        capture.delete_shot(job_id)
        return jsonify({"ok": True, "deleted": True})
    if not store.set_status(job_id, "new"):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "deleted": False})


@app.route("/api/stats")
def api_stats():
    stats = store.stats()
    stats["scrape"] = SCRAPE
    stats["statuses"] = STATUSES
    stats["vocabulary"] = classify.choices()
    configuration = cfg()
    own = configuration.rules()
    stats["own_rules"] = [r.to_dict() for r in own]
    stats["own_rule_counts"] = store.rule_counts([r for r in own if r.facet])
    stats["account"] = {"signed_in": bool(current_account()),
                        "has_profile": bool(derived_profile())}
    home = configuration.home_country()
    register = sponsorship.register_for(home.code if home else None)
    stats["sponsorship"] = {
        "mode": configuration.search.visa_sponsorship,
        "supported": register is not None,
        "market": home.name if home else None,
        "register": register.status() if register else None,
    }
    return jsonify(stats)


@app.route("/api/followups")
def api_followups():
    """Applications sitting quiet 21+ days - the "did I get ghosted" list.

    Same threshold the exported workbook's own ALERT column already uses, so the two
    never disagree; this is just that same nudge surfaced where you are actually
    looking, rather than only in a spreadsheet you have to remember to open.
    """
    jobs = store.follow_ups()
    for job in jobs:
        job["description"] = (job.get("description") or "")[:200]
    return jsonify({"jobs": jobs, "count": len(jobs), "threshold_days": store.FOLLOW_UP_DAYS})


# --------------------------------------------------------------------- config
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    configuration = cfg()
    if request.method == "GET":
        data = configuration.to_yaml_dict()
        data["profile"].pop("extra_skills", None)
        home = configuration.home_country()
        data["countries"] = geo.choices()
        data["home"] = ({"code": home.code, "name": home.name, "currency": home.currency,
                         "adzuna": bool(home.adzuna), "region": home.region}
                        if home else None)
        return jsonify(data)

    payload = request.json or {}
    search = payload.get("search", {})
    for key in ("titles", "keywords_any", "keywords_excluded", "locations",
                "exclude_companies"):
        if key in search:
            setattr(configuration.search, key,
                    [s.strip() for s in search[key] if str(s).strip()])
    if "country" in search:
        configuration.search.country = str(search["country"] or "").strip().upper()
    if "visa_sponsorship" in search:
        mode = str(search["visa_sponsorship"] or "any").strip().lower()
        if mode in ("any", "prefer", "require"):
            configuration.search.visa_sponsorship = mode
    if "work_modes" in search:
        configuration.search.work_modes = [
            m for m in (search["work_modes"] or [])
            if str(m).lower() in ("remote", "hybrid", "onsite")]
    for key, norm in (("employment_types", classify.normalise_kind),
                      ("job_families", classify.normalise_family)):
        if key in search:
            setattr(configuration.search, key,
                    [norm(v) for v in (search[key] or []) if str(v).strip()])
    if "include_unclassified" in search:
        configuration.search.include_unclassified = bool(search["include_unclassified"])
    if "career_stage" in search:
        stage = str(search["career_stage"] or "").strip().lower()
        if stage in ("student", "graduate", "professional", "senior", "any", ""):
            configuration.search.career_stage = stage
    for key in ("remote_ok", "exclude_senior"):
        if key in search:
            setattr(configuration.search, key, bool(search[key]))
    # Bounded rather than raised: a stray non-numeric string here used to 500 the
    # whole endpoint (an empty request body is normal - _num's fallback to the current
    # value on bad input is what a form field clearing itself should do, not a crash),
    # and a valid-but-silly number (a negative "maximum age", a 500 "minimum score")
    # used to be accepted verbatim and quietly empty the results with no explanation.
    if "salary_min" in search:
        value = search["salary_min"]
        configuration.search.salary_min = (
            int(_num(value, 0, low=0)) if value not in (None, "") else None)
    if "max_age_days" in search:
        value = search["max_age_days"]
        configuration.search.max_age_days = (
            int(_num(value, 60, low=1, high=3650)) if value not in (None, "") else None)
    if "min_score" in payload:
        configuration.min_score = _num(payload["min_score"], configuration.min_score,
                                       low=0, high=100)
    for name, value in (payload.get("weights") or {}).items():
        if hasattr(configuration.weights, name):
            current = getattr(configuration.weights, name)
            setattr(configuration.weights, name, _num(value, current, low=0, high=100))

    save_cfg(configuration)
    result = rescore(configuration, store, derived_profile(),
                     prune=bool(payload.get("prune", True)))
    return jsonify({"ok": True, "rescore": result})


_VIEW_ORDERS = ("score", "date", "new", "closing", "company", "salary",
                "employment", "family")
_VIEW_LISTS = ("employment", "families", "rules")
_VIEW_FLAGS = ("starred", "remote", "sponsored", "salary_disclosed", "unclassified",
               "unviewed")


@app.route("/api/view", methods=["POST"])
def api_view():
    """Remember the Positions sidebar - search box, sliders, checkboxes - for next time.

    Separate from /api/config on purpose: that endpoint rescores the whole store,
    which is right for criteria and far too heavy to run on every checkbox click.
    Nothing here changes a stored row, so saving it is just a file write.
    """
    payload = request.get_json(silent=True) or {}
    configuration = cfg()
    view = configuration.view
    if "q" in payload:
        view.q = str(payload["q"] or "")[:200]
    if "min_score" in payload:
        view.min_score = _num(payload["min_score"], view.min_score, low=0, high=100)
    for key in ("status", "source"):
        if key in payload:
            setattr(view, key, str(payload[key] or "all")[:60])
    if payload.get("order") in _VIEW_ORDERS:
        view.order = payload["order"]
    if payload.get("startups") in ("any", "hide", "only"):
        view.startups = payload["startups"]
    for key in _VIEW_FLAGS:
        if key in payload:
            setattr(view, key, bool(payload[key]))
    for key in _VIEW_LISTS:
        if key in payload and isinstance(payload[key], list):
            setattr(view, key, [str(v)[:60] for v in payload[key][:50] if str(v).strip()])
    save_cfg(configuration)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ assistant
@app.route("/api/assistant", methods=["POST"])
def api_assistant():
    payload = request.json or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    configuration = cfg()
    result = assistant.handle(message, configuration, store,
                              view=payload.get("view") or {},
                              confirm=bool(payload.get("confirm")))
    data = result.to_dict()

    # A criteria change is only visible once the stored rows are re-scored.
    if result.config_changed:
        data["rescore"] = rescore(configuration, store, derived_profile(),
                                  prune=True)

    if result.view.get("run_scrape"):
        data["scrape_started"] = _start_scrape()
    if result.view.get("rescore"):
        data["rescore"] = rescore(configuration, store, derived_profile(),
                                  prune=True)
    if result.view.get("export"):
        data["export"] = _export(result.view["export"], view=payload.get("view"))

    return jsonify(data)


# --------------------------------------------------------------------- scrape
def _scrape_worker(configuration: Config, profile: dict | None) -> None:
    def progress(stage: str, info: dict) -> None:
        SCRAPE["stage"] = stage
        if stage == "ats":
            SCRAPE.update(done=info["done"], total=info["total"],
                          found=info["running_total"],
                          message=f"company boards - {info['company']}")
        elif stage == "aggregator":
            SCRAPE["message"] = f"job board - {info['source']} ({info.get('found', 0)})"
            SCRAPE["found"] = info.get("running_total", SCRAPE["found"])
        elif stage == "start":
            SCRAPE["message"] = info.get("message", "")
        elif stage == "scoring":
            SCRAPE["message"] = f"scoring {info['total']} postings"

    try:
        summary = run_pipeline(configuration, store, progress, profile)
        SCRAPE["summary"] = summary
        SCRAPE["message"] = (f"{summary['new']} new, {summary['kept']} kept "
                             f"from {summary['found']} found")
    except Exception as exc:
        SCRAPE["error"] = str(exc)
        log.error("scrape failed: %s\n%s", exc, traceback.format_exc())
    finally:
        SCRAPE["running"] = False
        SCRAPE["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _start_scrape() -> bool:
    # Both are read here, on the request thread, because the worker thread has no Flask
    # session and therefore no idea who is signed in - cfg() reads the session too, so
    # calling it from the worker crashed every scrape with "working outside of request
    # context". Handed over as thread arguments rather than parked in SCRAPE, which
    # /api/scrape/status returns to anyone, signed in or not.
    configuration = cfg()
    profile = derived_profile()
    with _scrape_lock:
        if SCRAPE["running"]:
            return False
        SCRAPE.update(running=True, stage="start", done=0, total=0, found=0,
                      message="starting", error=None, summary=None, finished_at=None)
    threading.Thread(target=_scrape_worker, args=(configuration, profile),
                     daemon=True).start()
    return True


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    started = _start_scrape()
    return jsonify({"started": started, "already_running": not started})


@app.route("/api/scrape/status")
def api_scrape_status():
    return jsonify(SCRAPE)


@app.route("/api/rescore", methods=["POST"])
def api_rescore():
    prune = bool((request.json or {}).get("prune", True))
    return jsonify(rescore(cfg(), store, derived_profile(), prune=prune))


# --------------------------------------------------------------------- export
def _export(mode: str, job_ids: list[str] | None = None,
            view: dict | None = None) -> dict:
    """Export the current view by default - not the whole database."""
    if mode == "tracker":                   # everything on the in-app tracker
        jobs, mode = store.tracked(), "new"
    elif job_ids:
        jobs = [j for j in (store.get(i) for i in job_ids) if j]
    else:
        view = view or {}
        jobs = store.query(
            status=view.get("status", "all"),
            source=view.get("source", "all"),
            min_score=float(view.get("min_score") or 0),
            search=(view.get("q") or "").strip(),
            starred_only=bool(view.get("starred")),
            remote_only=bool(view.get("remote")),
            sponsored_only=bool(view.get("sponsored")),
            employment=view.get("employment") or [],
            families=view.get("families") or [],
            include_unclassified=view.get("unclassified", True),
            own_rules=active_rules(view.get("rules") or []),
            order=view.get("order", "score"),
            limit=int(view.get("limit") or 400),
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if mode == "append":
        configuration = cfg()
        source = (configuration.profile.tracker_path or "").strip()
        if not source:
            return {"error": "Set profile.tracker_path in config.yaml to the workbook "
                             "you want rows appended to."}
        out = OUT_DIR / f"{Path(source).stem}_updated_{stamp}.xlsx"
        try:
            result = tracker.append_into(jobs, source, out,
                                         home=configuration.home_country())
        except FileNotFoundError:
            return {"error": f"could not find your workbook at {source}"}
    else:
        out = OUT_DIR / f"JobScout_Tracker_{stamp}.xlsx"
        result = tracker.export_new(jobs, out, home=cfg().home_country())

    result["filename"] = Path(result["path"]).name
    result["mode"] = mode
    return result


@app.route("/api/export", methods=["POST"])
def api_export():
    payload = request.json or {}
    return jsonify(_export(payload.get("mode", "new"), payload.get("job_ids"),
                           payload.get("view")))


@app.route("/api/download/<path:filename>")
def api_download(filename: str):
    return send_from_directory(OUT_DIR, filename, as_attachment=True)




# -------------------------------------------------------------------- accounts
@app.route("/api/account")
def api_account():
    """Who is signed in, and what signing in does. Safe to call when signed out."""
    account = current_account()
    return jsonify({
        "signed_in": bool(account),
        "username": account["username"] if account else "",
        "since": account["created_at"] if account else "",
        "any_accounts": ACCOUNTS.count() > 0,
        "known_usernames": ACCOUNTS.usernames()[:6],
        "has_profile": bool(derived_profile()),
        "what_is_kept": ["a username you invent", "a scrypt hash of your password",
                         "the date you created it"],
        "note": "There is no email field, so there is no password reset by email. "
                "Signing in unlocks your profile and criteria; the job list is public "
                "adverts and is not hidden either way.",
    })


@app.route("/api/account/create", methods=["POST"])
def api_account_create():
    payload = request.json or {}
    # Read before create() inserts the new row, or this would always see >= 1.
    is_first_account = ACCOUNTS.count() == 0
    try:
        account = ACCOUNTS.create(payload.get("username", ""), payload.get("password", ""))
    except accounts.AccountError as exc:
        return jsonify({"error": str(exc)}), 400

    # Seed the FIRST account on this install from whatever criteria are already set in
    # the shared config.yaml, so signing up right after tuning a search signed-out does
    # not wipe that work - it is overwhelmingly the same person. A second or later
    # account does not get this: at that point config.yaml reflects whoever last used
    # the machine signed out, which on a shared computer is not reliably this new
    # person, and silently copying it into their "own" account would leak their
    # titles, locations and keywords across users without either of them asking for it.
    target = ACCOUNTS.config_path(account["id"])
    if is_first_account and not target.exists():
        Config.load().save(target)
    session["account_id"] = account["id"]
    session.permanent = True
    return jsonify({"ok": True, "username": account["username"]})


@app.route("/api/account/login", methods=["POST"])
def api_account_login():
    payload = request.json or {}
    username = payload.get("username", "")
    if _login_rate_limited(username):
        return jsonify({"error": f"Too many attempts for that username. Wait a few "
                                 f"minutes and try again."}), 429
    account = ACCOUNTS.verify(username, payload.get("password", ""))
    if not account:
        _record_login_failure(username)
        # One message for both failures, so it cannot be used to enumerate usernames.
        return jsonify({"error": "That username and password do not match."}), 401
    _clear_login_failures(username)
    session["account_id"] = account["id"]
    session.permanent = True
    return jsonify({"ok": True, "username": account["username"]})


@app.route("/api/account/logout", methods=["POST"])
def api_account_logout():
    session.pop("account_id", None)
    drop_pending()
    return jsonify({"ok": True})


@app.route("/api/account/password", methods=["POST"])
def api_account_password():
    account = current_account()
    if not account:
        return jsonify({"error": "Not signed in."}), 401
    if _login_rate_limited(account["username"]):
        return jsonify({"error": "Too many attempts. Wait a few minutes and try "
                                 "again."}), 429
    payload = request.json or {}
    try:
        ACCOUNTS.change_password(account["id"], payload.get("current", ""),
                                 payload.get("new", ""))
    except accounts.AccountError as exc:
        _record_login_failure(account["username"])
        return jsonify({"error": str(exc)}), 400
    _clear_login_failures(account["username"])
    return jsonify({"ok": True})


@app.route("/api/account/delete", methods=["POST"])
def api_account_delete():
    account = current_account()
    if not account:
        return jsonify({"error": "Not signed in."}), 401
    if _login_rate_limited(account["username"]):
        return jsonify({"error": "Too many attempts. Wait a few minutes and try "
                                 "again."}), 429
    try:
        ACCOUNTS.delete(account["id"], (request.json or {}).get("password", ""))
    except accounts.AccountError as exc:
        _record_login_failure(account["username"])
        return jsonify({"error": str(exc)}), 400
    session.pop("account_id", None)
    return jsonify({"ok": True, "deleted": ["your login", "your derived profile",
                                            "your saved criteria"]})


# --------------------------------------------------------------------- profile
@app.route("/api/profile")
def api_profile():
    """The derived profile, plus exactly what was and was not taken from the import."""
    stored = derived_profile() or pending_profile()
    profile = matching_profile()
    profile_dict = stored or (profile.to_dict() if profile else None)
    configuration = cfg()

    # A link into LinkedIn's OWN alumni tool - "people from this school, and where they
    # work" - which only exists as a page a signed-in human can browse, not an API.
    # This is the honest version of "scrape my uni's alumni employers": open it
    # yourself, logged into your own account, rather than have the app fetch it
    # unauthenticated (which would just hit a login wall) or in violation of LinkedIn's
    # terms either way.
    school = (profile_dict or {}).get("institution") or configuration.profile.school
    school_slug = (configuration.profile.school_linkedin_slug
                  or (company_slug(school) if school else ""))
    school_alumni_url = (f"https://www.linkedin.com/school/{school_slug}/people/"
                         if school_slug else "")

    return jsonify({
        "profile": profile_dict,
        "source": (stored or {}).get("source") or (profile.source if profile else ""),
        "signed_in": bool(current_account()),
        "stored": bool(derived_profile()),
        "extra_skills": configuration.profile.extra_skills,
        "suggestions": linkedin_import.suggestions(stored) if stored else None,
        # Informational only - never applied as a filter. See target_families' own
        # docstring for why: an earlier version of this used a person's past jobs as a
        # hard filter on the search and it hid 1,038 of 1,833 postings by accident.
        "target_families": (linkedin_import.target_families(profile_dict)
                            if profile_dict else []),
        "school": school,
        "school_alumni_url": school_alumni_url,
        "kept": sorted(privacy.PROFILE_ALLOWLIST),
        "never_kept": sorted(privacy.NEVER_STORED),
        "how_to_import": {
            "best": {
                "what": "Your LinkedIn data export (a .zip)",
                "where": "LinkedIn > Settings & Privacy > Data privacy > "
                         "Get a copy of your data. Pick the full archive, not the "
                         "connections-only one. It arrives by email in minutes.",
                "why": "Structured - LinkedIn separates your skills, dates, degree and "
                       "field of study, so nothing has to be guessed.",
            },
            "quicker": {
                "what": "Your profile as a PDF",
                "where": "Your own LinkedIn profile, More, then Save to PDF.",
                "why": "One click. Less precise: no dates means no experience total.",
            },
            "why_not_a_url": "Pasting your profile URL cannot work. LinkedIn requires a "
                             "login to read a profile and their terms forbid automated "
                             "access, and their official sign-in API returns your name, "
                             "email and photo but none of your experience. The export is "
                             "the route that is both allowed and complete.",
        },
    })


# Default to early-career postings, or "which companies hire the most people overall"
# would dominate the list with whoever posts the most senior/volume roles.
_EARLY_CAREER_KINDS = ["internship", "placement", "graduate_scheme", "apprenticeship"]


@app.route("/api/profile/review")
def api_profile_review():
    """How complete the SIGNED-IN user's own LinkedIn profile is, plus tips.

    Same account/session pattern as api_profile(): a signed-in account's stored profile,
    or a not-yet-saved import held for this session only. Never anyone else's profile -
    there is no path in this app that takes another person's URL or ID.

    Calls linkedin_import.profile_review with tables=None - option (a) from this
    feature's brief, chosen deliberately over re-reading the original export file.
    api_profile_import() never keeps a path or the raw export tables anywhere after
    import (see below it in this file), and the honest way to keep it that way is to not
    add a new place that holds them, even a session-only one. The trade-off - two of the
    seven official sections (photo, summary) and a couple of the bonus quality checks
    (summary length, per-role descriptions) can't be assessed without the raw text - is
    real, and profile_review() says so plainly in the "note" it returns rather than this
    route papering over it. The fuller-fidelity path (tables passed through) exists in
    profile_review itself and is exercised in tools/audit.py against a fixture export,
    for whenever this app decides the trade-off is worth revisiting.
    """
    stored = derived_profile() or pending_profile()
    if not stored:
        return jsonify({"review": None,
                        "note": "Import your LinkedIn first, on this tab."})
    return jsonify({"review": linkedin_import.profile_review(stored, tables=None)})


@app.route("/api/profile/employers")
def api_profile_employers():
    """Companies actually hiring in your field, ranked by real postings we hold.

    Not alumni data - nobody publishes an API for "who a school's graduates work for",
    and LinkedIn's own version of that view is only visible to a signed-in person
    browsing it themselves (linked from /api/profile as school_alumni_url). This is the
    honest substitute: real counts from postings this install has actually scraped.
    """
    stored = derived_profile() or pending_profile()
    profile = matching_profile()
    profile_dict = stored or (profile.to_dict() if profile else None)

    requested = [f.strip() for f in request.args.get("families", "").split(",") if f.strip()]
    families = requested or (linkedin_import.target_families(profile_dict)
                             if profile_dict else [])
    valid_families = {f["key"] for f in classify.choices()["families"]}
    families = [f for f in families if f in valid_families]

    all_kinds = request.args.get("all_kinds") == "1"
    kinds = None if all_kinds else _EARLY_CAREER_KINDS

    return jsonify({
        "families": families,
        "kinds": kinds,
        "employers": store.top_employers(families, kinds, limit=12) if families else [],
        "note": ("No field of work could be inferred from your profile - add your "
                 "course or import your LinkedIn on this tab, or pass ?families=... "
                 "yourself." if not families else ""),
    })


@app.route("/api/profile/import", methods=["POST"])
def api_profile_import():
    """Read a LinkedIn export, a profile PDF, or pasted profile text."""
    payload = request.json or {}
    path = (payload.get("path") or "").strip().strip('"')
    text = payload.get("text") or ""

    try:
        if path:
            imported = linkedin_import.from_file(path)
        elif text.strip():
            imported = linkedin_import.derive_from_text(text, source="linkedin_text")
        else:
            return jsonify({"error": "Give a path to your export .zip or profile .pdf, "
                                     "or paste your profile text."}), 400
    except FileNotFoundError:
        return jsonify({"error": "Could not find " + path}), 400
    except Exception as exc:
        log.warning("profile import failed: %s", exc)
        return jsonify({"error": str(exc)}), 400

    if not imported.get("skills") and not imported.get("education_level"):
        return jsonify({"error": "Read the file, but found no skills or qualifications "
                                 "in it. If this was the connections-only export, "
                                 "request the full archive instead."}), 400

    account = current_account()
    if account:
        ACCOUNTS.save_profile(account["id"], imported)
    else:
        # Nowhere durable to put it without an account, so say so rather than pretending.
        hold_pending(imported)

    return jsonify({
        "ok": True, "profile": imported,
        "suggestions": linkedin_import.suggestions(imported),
        "stored": bool(account),
        "note": ("Saved to your account." if account else
                 "Held for this session only. Create an account on the Profile tab to "
                 "keep it."),
    })


@app.route("/api/profile/apply", methods=["POST"])
def api_profile_apply():
    """Accept the inferred search criteria. Nothing is applied until this is called."""
    stored = derived_profile() or pending_profile()
    if not stored:
        return jsonify({"error": "Import your LinkedIn first."}), 400
    wanted = set((request.json or {}).get("fields")
                 or ["titles", "keywords_any", "locations", "country", "career_stage",
                     "exclude_senior"])
    suggested = linkedin_import.suggestions(stored)

    configuration = cfg()
    applied = {}
    for name in ("titles", "keywords_any", "locations", "job_families"):
        if name in wanted and suggested.get(name):
            # Merge rather than replace: the user may have curated these already.
            existing = list(getattr(configuration.search, name) or [])
            added = [v for v in suggested[name] if v not in existing]
            setattr(configuration.search, name, existing + added)
            applied[name] = added
    if "country" in wanted and suggested.get("country"):
        configuration.search.country = suggested["country"]
        applied["country"] = suggested["country"]
    if "career_stage" in wanted and suggested.get("career_stage"):
        configuration.search.career_stage = suggested["career_stage"]
        applied["career_stage"] = suggested["career_stage"]
    if "exclude_senior" in wanted:
        configuration.search.exclude_senior = bool(suggested.get("exclude_senior"))
        applied["exclude_senior"] = configuration.search.exclude_senior

    # The institution is a public affiliation and is what makes alumni outreach work.
    if stored.get("institution"):
        configuration.profile.school = stored["institution"]
        configuration.profile.school_linkedin_slug = company_slug(stored["institution"])
    if stored.get("graduation_year"):
        configuration.profile.graduation_year = stored["graduation_year"]

    save_cfg(configuration)
    return jsonify({"ok": True, "applied": applied,
                    "rescore": rescore(configuration, store, stored, prune=True)})


@app.route("/api/profile/forget", methods=["POST"])
def api_profile_forget():
    """Delete the derived profile. Criteria and the job list are untouched."""
    account = current_account()
    drop_pending()
    removed = ACCOUNTS.forget_profile(account["id"]) if account else False
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/profile/skills", methods=["POST"])
def api_profile_skills():
    """Add or remove skills the built-in vocabulary does not know.

    Yours to set. The vocabulary covers about three hundred technical terms and will
    never cover a niche instrument or an in-house tool, so anything typed here scores
    exactly like a recognised skill.
    """
    payload = request.json or {}
    configuration = cfg()
    current = list(configuration.profile.extra_skills or [])
    for skill in (payload.get("add") or []):
        clean = str(skill).strip().lower()[:48]
        if clean and clean not in current:
            current.append(clean)
    for skill in (payload.get("remove") or []):
        clean = str(skill).strip().lower()
        if clean in current:
            current.remove(clean)
    configuration.profile.extra_skills = current
    save_cfg(configuration)
    return jsonify({"ok": True, "extra_skills": current,
                    "rescore": rescore(configuration, store, derived_profile(),
                                       prune=False)})


# ---------------------------------------------------------------- custom rules
@app.route("/api/rules", methods=["GET", "POST"])
def api_rules():
    """The user's own filters. Anything the built-in facets do not cover goes here."""
    configuration = cfg()
    if request.method == "GET":
        own = configuration.rules()
        return jsonify({
            "rules": [r.to_dict() for r in own],
            "counts": store.rule_counts(own),
            "count_means": "For a facet, how many visible postings it would show. For "
                           "an exclude or require rule, how many postings it is "
                           "currently hiding.",
            "modes": [
                {"mode": "exclude", "what": "Never show postings that mention this"},
                {"mode": "require", "what": "Only show postings that mention this"},
                {"mode": "boost", "what": "Rank these higher, but still show the rest"},
            ],
            "fields": list(rules_mod.FIELDS),
        })

    payload = request.json or {}
    incoming = payload.get("rules")
    if not isinstance(incoming, list):
        return jsonify({"error": "rules must be a list"}), 400
    # Round-trip through the parser so a malformed rule is rejected before it is saved.
    parsed = rules_mod.parse(incoming)
    configuration.search.custom_rules = [r.to_dict() for r in parsed]
    save_cfg(configuration)
    # Rescore first: the counts must describe the database as it will be after the new
    # rules have hidden or restored rows, not as it was a moment before.
    result = rescore(configuration, store, derived_profile(),
                     prune=bool(payload.get("prune", True)))
    return jsonify({"ok": True, "rules": [r.to_dict() for r in parsed],
                    "counts": store.rule_counts(parsed), "rescore": result})


# ------------------------------------------------------------------ why / prep
@app.route("/api/job/<job_id>/why")
def api_why(job_id: str):
    """Why this posting suits you - the line under the swipe card."""
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    job = _job_from_row(row)
    use_model = request.args.get("model", "1") == "1"
    return jsonify(why_mod.explain(job, matching_profile(), use_model=use_model))


@app.route("/api/job/<job_id>/interview")
def api_interview(job_id: str):
    """Interview and assessment prep for this exact posting."""
    row = store.get(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    job = _job_from_row(row)

    try:
        company = enrich.company_profile(row["company"], job_url=row["url"])
    except Exception as exc:
        log.warning("enrichment failed for the interview brief: %s", exc)
        company = None

    brief = interview.build_brief(
        job, matching_profile(), company,
        kind=row.get("employment_kind") or "",
        family=row.get("job_family") or "",
        use_model=request.args.get("model", "1") == "1")
    return jsonify(brief.to_dict())


# --------------------------------------------------------------------- privacy
@app.route("/api/privacy")
def api_privacy():
    """What this install holds, where, and whether it is personal."""
    return jsonify({
        "inventory": privacy.inventory(ROOT),
        "kept": sorted(privacy.PROFILE_ALLOWLIST),
        "never_kept": sorted(privacy.NEVER_STORED),
        "principles": [
            "Nothing is taken from you that is not already public on your own LinkedIn.",
            "The export you hand over holds your name, email, phone, address and "
            "connections. None of those are read, and there is no field to store them in.",
            "A CV, if you point at one, is read for skills and the qualification. The "
            "text is not kept and the file is not copied.",
            "The tracker holds employers' advert data and your own notes. It has no "
            "column for anything about you.",
            "Nothing leaves the machine unless you ask. Adverts and your public skills "
            "go to Claude only when you open a brief or a match reason, and only if you "
            "have set an API key.",
            "No analytics, no telemetry, no account on anybody's server.",
        ],
    })


@app.route("/api/privacy/purge", methods=["POST"])
def api_privacy_purge():
    """Delete a named store. Explicit per store: no single button wipes everything."""
    target = (request.json or {}).get("target", "")
    account = current_account()
    if target == "profile":
        drop_pending()
        removed = ACCOUNTS.forget_profile(account["id"]) if account else False
        return jsonify({"ok": True,
                        "removed": "derived profile" if removed else "nothing to remove"})
    if target == "cache":
        import shutil
        cache = ROOT / "data" / "cache"
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        cache.mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True, "removed": "page cache"})
    if target == "notes":
        count = store.clear_notes()
        return jsonify({"ok": True, "removed": str(count) + " notes"})
    return jsonify({"error": "target must be one of: profile, cache, notes"}), 400


if __name__ == "__main__":
    print("\n  Job Scout  ->  http://127.0.0.1:5000\n")
    # threaded=True matters here, not just for raw throughput: without it Werkzeug's
    # dev server handles one request at a time, so a single slow request - an
    # interview brief probing an employer's website, a scrape in progress - freezes
    # every other tab and the stats poll along with it. Store and Accounts already
    # hand out a connection per thread for exactly this.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
