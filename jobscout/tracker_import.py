"""Read the user's own application spreadsheet into the in-app tracker.

People keep these in every shape: the workbook this app exports (a row of group labels
above the headers), a hand-made sheet with a typo in "Company", a CSV out of Google
Sheets. So the header row is found rather than assumed, headers are matched by meaning
rather than spelling, and nothing in a row is thrown away - columns the tracker has no
place for are kept alongside the row and written back out on export.

Importing the same file twice changes nothing the second time.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime

from .models import Job, clean
from .store import APP_STATUSES, Store

MAX_BYTES = 5 * 1024 * 1024
SOURCE = "my_tracker"

# field -> header spellings, compared after lower-casing and dropping everything that
# is not a letter or digit ("Applied?" -> "applied", "2nd / AC" -> "2ndac").
ALIASES = {
    "company": ["company", "comapny", "compnay", "companyname", "employer", "organisation",
                "organization", "firm", "bank"],
    "title": ["rolename", "role", "jobtitle", "title", "position", "job", "jobname",
              "programme", "program", "roletitle", "positiontitle", "vacancy"],
    "url": ["applicationlink", "link", "url", "applylink", "joblink", "applicationurl",
            "joburl", "website", "posting", "listing"],
    "deadline": ["deadline", "closingdate", "closes", "applicationdeadline", "closedate",
                 "duedate", "applyby", "closing"],
    "location": ["location", "city", "office", "town", "place"],
    "salary": ["salary", "pay", "wage", "compensation"],
    "applied_flag": ["applied", "appliedyn", "haveiapplied", "submitted"],
    "applied_at": ["dateapplied", "applieddate", "applicationdate", "appliedon",
                   "datesubmitted", "submittedon"],
    "status": ["status", "stage", "applicationstatus", "progress", "outcome", "result"],
    "notes": ["notes", "note", "comments", "comment", "thoughts"],
    # kept as-is and written back to the same columns of the exported workbook
    "Duration": ["duration", "length"],
    "Start Date": ["startdate", "start", "starts"],
    "Open Date": ["opendate", "opens", "openingdate", "dateopened", "posted", "dateposted"],
    "Source": ["source", "foundon", "foundvia", "where", "site"],
    "CV": ["cv", "resume"],
    "Cover Letter": ["coverletter", "cl"],
    "Got as far as": ["gotasfaras", "furtheststage"],
    "Next Action": ["nextaction", "nextstep", "todo"],
    "Next Action Date": ["nextactiondate", "nextstepdate", "followupdate"],
}
# Formula columns of the exported workbook - recomputed there, nothing to keep.
IGNORED = {"daysleft", "alert"}
EXTRA_DATES = {"Start Date", "Open Date", "Next Action Date"}
# A loose second pass for headers like "Company (parent)" or "Job title / team".
CONTAINS = [("company", "compan"), ("title", "role"), ("title", "title"), ("url", "link"),
            ("url", "url"), ("deadline", "deadline"), ("status", "status"),
            ("notes", "note"), ("applied_at", "dateapplied")]

_STATUS_EXACT = {s.lower(): s for s in APP_STATUSES}
_STATUS_WORDS = {
    "Not applied yet": ["not applied", "notapplied", "to apply", "todo", "to do",
                        "not started", "planning", "planned", "interested", "saved",
                        "shortlisted", "wishlist", "researching", "draft", "drafting"],
    "Applied": ["applied", "submitted", "sent", "pending", "waiting", "in review",
                "under review", "awaiting", "awaiting response"],
    "Online test": ["online test", "oa", "online assessment", "test", "tests", "hirevue",
                    "video interview", "game", "games", "psychometric", "assessment",
                    "numerical test", "situational judgement"],
    "1st interview": ["interview", "1st interview", "first interview", "phone interview",
                      "phone screen", "screen", "screening", "interviewing"],
    "2nd / AC": ["2nd interview", "second interview", "ac", "assessment centre",
                 "assessment center", "2nd / ac", "2nd/ac", "superday"],
    "Final round": ["final", "final round", "final interview", "partner interview"],
    "Offer": ["offer", "offered", "offer received"],
    "Accepted": ["accepted", "accepted offer", "signed"],
    "Rejected": ["rejected", "unsuccessful", "declined", "rejection"],
    "Ghosted": ["ghosted", "no response", "no reply", "heard nothing"],
    "Withdrawn": ["withdrawn", "withdrew", "not interested", "pulled out"],
}
_STATUS_SYNONYM = {w: stage for stage, words in _STATUS_WORDS.items() for w in words}
# Checked in this order, so "assessment centre" wins over "assessment".
_STATUS_CONTAINS = [("reject", "Rejected"), ("unsuccess", "Rejected"),
                    ("ghost", "Ghosted"), ("withdr", "Withdrawn"),
                    ("accepted", "Accepted"), ("offer", "Offer"), ("final", "Final round"),
                    ("assessment cent", "2nd / AC"), ("2nd", "2nd / AC"),
                    ("second", "2nd / AC"), ("interview", "1st interview"),
                    ("test", "Online test"), ("assessment", "Online test"),
                    ("applied", "Applied"), ("submitted", "Applied")]


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_ALIAS_LOOKUP = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


def match_headers(row: list) -> dict[int, str]:
    """Column index -> field for one candidate header row."""
    found: dict[int, str] = {}
    taken: set[str] = set()
    for i, cell in enumerate(row):
        field = _ALIAS_LOOKUP.get(_norm(cell))
        if field and field not in taken:
            found[i] = field
            taken.add(field)
    for i, cell in enumerate(row):
        if i in found or not isinstance(cell, str) or _norm(cell) in IGNORED:
            continue
        low = _norm(cell)
        for field, needle in CONTAINS:
            if needle in low and field not in taken:
                found[i] = field
                taken.add(field)
                break
    return found


def parse_status(value) -> str | None:
    """A tracker stage for whatever the spreadsheet says, or None if it is unclear."""
    text = clean(str(value or "")).lower()
    if not text:
        return None
    if text in _STATUS_EXACT:
        return _STATUS_EXACT[text]
    if text in _STATUS_SYNONYM:
        return _STATUS_SYNONYM[text]
    for needle, stage in _STATUS_CONTAINS:
        if needle in text:
            return stage
    for word in re.findall(r"[a-z]+", text):         # "OA sent", "pending feedback"
        if word in _STATUS_SYNONYM and len(word) > 1:
            return _STATUS_SYNONYM[word]
    return None


def parse_flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = clean(str(value or "")).lower()
    if text in ("true", "yes", "y", "x", "1", "done", "✓", "✔"):
        return True
    if text in ("false", "no", "n", "0", ""):
        return False if text else None
    return None


def parse_day(value) -> str:
    """YYYY-MM-DD, reading strings the British way (dd/mm/yyyy) as the workbook does."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean(str(value or ""))
    if not text:
        return ""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y",
                "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%d %B", "%d %b",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(text.replace(",", ""), fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:          # "12 Oct" - assume next one
            today = date.today()
            parsed = parsed.replace(year=today.year)
            if parsed.date() < today.replace(month=1, day=1):
                parsed = parsed.replace(year=today.year + 1)
        return parsed.date().isoformat()
    return ""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (datetime, date)):
        return parse_day(value)
    return clean(str(value))


# ---------------------------------------------------------------------- reading
def read_rows(data: bytes, filename: str) -> tuple[str, list[list]]:
    """(sheet name, rows) of the sheet that looks most like an application tracker."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return "CSV", [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    if name.endswith(".xls"):
        raise ValueError("That is the old .xls format. Open it in Excel and save it as "
                         ".xlsx, then upload that.")
    if not name.endswith((".xlsx", ".xlsm")):
        raise ValueError("Upload an Excel workbook (.xlsx) or a .csv file.")
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Could not open that workbook: {exc}") from exc
    try:
        best: tuple[int, str, list[list]] | None = None
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(max_row=5000, values_only=True)]
            score = max((_header_score(r) for r in rows[:15]), default=0)
            if best is None or score > best[0]:
                best = (score, ws.title, rows)
        return (best[1], best[2]) if best else ("", [])
    finally:
        wb.close()


def _header_score(row: list) -> int:
    fields = set(match_headers(row).values())
    return len(fields) + (10 if "company" in fields else 0)


# ---------------------------------------------------------------------- importing
def _ext_id(company: str, title: str) -> str:
    key = f"{_norm(company)}|{_norm(title)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def add_manual(store: Store, *, company: str, title: str, url: str = "",
               location: str = "", deadline: str = "", salary: str = "",
               posted: str = "") -> tuple[str, bool]:
    """Put a job the user found elsewhere into the database. (id, created)."""
    company, title = clean(company), clean(title) or "(no role name)"
    job = Job(source=SOURCE, source_kind="manual", company=company, title=title,
              url=clean(url), external_id=_ext_id(company, title), location=location,
              salary_raw=salary, closes_at=deadline or None, posted_at=posted or None)
    existing = _find(store, job)
    if existing:
        return existing["id"], False
    store.upsert([job])
    return job.id, True


def _find(store: Store, job: Job) -> dict | None:
    conn = store.conn
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    if row is None and job.url:
        bare = job.url.rstrip("/")
        row = conn.execute("SELECT * FROM jobs WHERE url = ? OR url = ? LIMIT 1",
                           (bare, bare + "/")).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM jobs WHERE dedupe_key = ? LIMIT 1",
                           (job.dedupe_key,)).fetchone()
    return dict(row) if row else None


def import_tracker(store: Store, data: bytes, filename: str) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("That file is over 5 MB - is it the right one?")
    sheet, rows = read_rows(data, filename)

    header_at, columns = -1, {}
    for i, row in enumerate(rows[:15]):
        found = match_headers(row)
        if "company" in found.values() and len(found) > len(columns):
            header_at, columns = i, found
    if header_at < 0:
        raise ValueError("Could not find a header row with a Company column in the "
                         "first 15 rows. The tracker needs at least Company and, "
                         "ideally, Role.")
    headers = rows[header_at]
    other_cols = {i: _text(h) for i, h in enumerate(headers)
                  if i not in columns and _text(h) and _norm(h) not in IGNORED}

    added = updated = skipped = 0
    skipped_rows: list[int] = []
    unknown_status: dict[str, int] = {}
    for offset, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        got = {field: row[i] if i < len(row) else None for i, field in columns.items()}
        company, title = _text(got.get("company")), _text(got.get("title"))
        if not company:
            # template rows come pre-filled with "1 Year" and FALSE but no company
            if title:
                skipped += 1
                skipped_rows.append(offset)
            continue

        raw_status = _text(got.get("status"))
        flag = parse_flag(got.get("applied_flag"))
        applied_on = parse_day(got.get("applied_at"))
        stage = parse_status(raw_status)
        if stage is None and got.get("applied_flag") is not None and flag is None:
            stage = parse_status(got.get("applied_flag"))   # "Applied?" holding a stage
        notes = _text(got.get("notes"))
        if raw_status and stage is None:
            unknown_status[raw_status] = unknown_status.get(raw_status, 0) + 1
            notes = (f"Status in my spreadsheet: {raw_status}\n" + notes).strip()
        explicit = stage is not None
        if stage is None:
            stage = "Applied" if (flag or applied_on) else "Not applied yet"

        extra = {}
        for field in ALIASES:
            if field[0].isupper() and got.get(field) not in (None, ""):
                value = got[field]
                extra[field] = (parse_day(value) or _text(value)) if field in EXTRA_DATES \
                    else value if isinstance(value, bool) else _text(value)
        others = {h: _text(row[i]) for i, h in other_cols.items()
                  if i < len(row) and _text(row[i])}
        if others:
            extra["Other"] = others

        url = _text(got.get("url"))
        if url and not re.match(r"^https?://", url, re.I):
            url = "https://" + url if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", url) else ""
        fields = dict(company=company, title=title or "(no role name)", url=url,
                      location=_text(got.get("location")),
                      deadline=parse_day(got.get("deadline")),
                      salary=_text(got.get("salary")),
                      posted=extra.get("Open Date", ""))
        job_id, created = add_manual(store, **fields)
        current = store.get(job_id) or {}
        if not created and current.get("source_kind") == "manual":
            # the spreadsheet is the only source of truth for a row typed in by hand
            store.conn.execute(
                "UPDATE jobs SET url = ?, location = ?, closes_at = ?, salary_display = ? "
                "WHERE id = ?", (fields["url"], fields["location"],
                                 fields["deadline"] or None, fields["salary"], job_id))

        was_tracked = bool(current.get("tracked"))
        if explicit or not was_tracked or (stage != "Not applied yet"
                                       and current.get("app_status") in ("", "Not applied yet")):
            store.set_app_status(job_id, stage)
        # set_app_status stamps today as the date applied; the sheet's date, or none,
        # is the truth for an application made before it was imported
        store.set_applied_at(job_id, applied_on or current.get("applied_at") or "")
        if applied_on and not was_tracked:
            # so "gone quiet" counts from when they applied, not from the import
            store.conn.execute(
                "UPDATE jobs SET status_changed_at = ? WHERE id = ? AND status = 'applied'",
                (applied_on + "T00:00:00+00:00", job_id))
        if fields["deadline"] and not current.get("closes_at"):
            store.conn.execute("UPDATE jobs SET closes_at = ? WHERE id = ?",
                               (fields["deadline"], job_id))
        old_notes = (current.get("notes") or "").strip()
        if notes and notes not in old_notes:
            store.set_note(job_id, f"{old_notes}\n{notes}".strip() if old_notes else notes)
        if extra:
            merged = {**(current.get("tracker_extra") or {}), **extra}
            store.set_tracker_extra(job_id, merged)
        store.conn.commit()
        if created:
            added += 1
        else:
            updated += 1

    return {"ok": True, "sheet": sheet, "header_row": header_at + 1,
            "columns": {_text(headers[i]): f for i, f in sorted(columns.items())},
            "kept_extra_columns": list(other_cols.values()),
            "added": added, "updated": updated, "skipped": skipped,
            "skipped_rows": skipped_rows[:20], "unknown_statuses": unknown_status}
