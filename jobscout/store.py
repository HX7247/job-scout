"""SQLite persistence: dedupe across sources, remember what's new, track applications."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "jobscout.db"

STATUSES = ["new", "shortlisted", "applied", "interviewing", "offer", "rejected", "dismissed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    location        TEXT,
    remote          INTEGER DEFAULT 0,
    department      TEXT,
    employment_type TEXT,
    description     TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_display  TEXT,
    posted_at       TEXT,
    closes_at       TEXT,
    score           REAL DEFAULT 0,
    score_reasons   TEXT,
    matched_skills  TEXT,
    missing_skills  TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    notes           TEXT DEFAULT '',
    starred         INTEGER DEFAULT 0,
    filtered        INTEGER DEFAULT 0,
    filter_reason   TEXT DEFAULT '',
    sponsor_name    TEXT DEFAULT '',
    sponsor_rating  TEXT DEFAULT '',
    sponsor_routes  TEXT DEFAULT '',
    sponsor_conf    REAL DEFAULT 0,
    employment_kind TEXT DEFAULT '',
    job_family      TEXT DEFAULT '',
    status_changed_at TEXT DEFAULT '',
    viewed_at       TEXT DEFAULT '',
    startup         INTEGER DEFAULT NULL,
    delisted_at     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    found      INTEGER, kept INTEGER, new INTEGER,
    detail     TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # One connection per thread. Flask serves each request on its own thread, and a
    # single sqlite3 connection shared between them is not safe - it surfaced as
    # "database is locked" and "bad parameter or other API misuse" under the handful of
    # parallel requests the page makes on load.
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            # WAL lets readers continue while a write is in flight, which is the whole
            # access pattern here: many small reads, occasional bulk write.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            self._opened.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this object opened, on any thread."""
        for conn in self._opened:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._opened.clear()
        self._local = threading.local()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        Indexes on those columns are created here rather than in SCHEMA, because
        SCHEMA runs against the pre-migration table on an existing database.
        """
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, ddl in (("filtered", "INTEGER DEFAULT 0"),
                            ("filter_reason", "TEXT DEFAULT ''"),
                            ("sponsor_name", "TEXT DEFAULT ''"),
                            ("sponsor_rating", "TEXT DEFAULT ''"),
                            ("sponsor_routes", "TEXT DEFAULT ''"),
                            ("sponsor_conf", "REAL DEFAULT 0"),
                            ("employment_kind", "TEXT DEFAULT ''"),
                            ("job_family", "TEXT DEFAULT ''"),
                            ("status_changed_at", "TEXT DEFAULT ''"),
                            ("viewed_at", "TEXT DEFAULT ''"),
                            ("startup", "INTEGER DEFAULT NULL"),
                            ("delisted_at", "TEXT DEFAULT ''")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_filtered ON jobs(filtered)")
        self._scrub_run_profiles()

    def _scrub_run_profiles(self) -> None:
        """Pass profiles saved inside old run records through the privacy allowlist.

        Runs recorded before privacy.py existed stored the whole parsed CV under "cv" -
        email, LinkedIn slug, file path, raw text - and /api/stats serves the latest
        run's detail to the page, signed in or not. New runs already go through the
        allowlist (CVProfile.to_dict); this brings the old ones into line, and is a
        no-op once they are clean.
        """
        from .privacy import PROFILE_ALLOWLIST, clean_profile_dict
        rows = self.conn.execute("SELECT id, detail FROM runs").fetchall()
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(detail, dict):
                continue
            changed = False
            for key in ("cv", "profile"):
                sub = detail.get(key)
                if isinstance(sub, dict) and set(sub) - PROFILE_ALLOWLIST - {"career_stage"}:
                    cleaned = clean_profile_dict(sub)
                    if "career_stage" in sub:
                        cleaned["career_stage"] = sub["career_stage"]
                    detail[key] = cleaned
                    changed = True
            if changed:
                self.conn.execute("UPDATE runs SET detail = ? WHERE id = ?",
                                  (json.dumps(detail), row["id"]))

    # ------------------------------------------------------------------ write
    def upsert(self, jobs: list[Job]) -> tuple[int, int]:
        """Insert or refresh. Returns (new_count, updated_count).

        A posting already stored under a different source (same dedupe_key) is treated
        as a duplicate and kept as one row. The employer's own board wins: its link and
        details are never replaced by an aggregator's copy, and an aggregator row is
        upgraded to the employer's link once the employer's board lists it.
        """
        new_count = updated = 0
        stamp = now()
        cur = self.conn.cursor()
        for job in jobs:
            existing = cur.execute(
                "SELECT id, score, source_kind FROM jobs WHERE id = ? OR dedupe_key = ? "
                "LIMIT 1",
                (job.id, job.dedupe_key),
            ).fetchone()
            if existing and existing["source_kind"] == "ats_direct" \
                    and job.source_kind != "ats_direct":
                # The employer's own board is the authority on its posting: its link
                # goes straight to the real Apply button and its description is the
                # full one. A copy from an aggregator or a job list only confirms the
                # posting is still live - it used to overwrite the employer's link with
                # its own (an Ashby posting ending up pointing at Arbeitnow).
                cur.execute("UPDATE jobs SET last_seen = ?, delisted_at = '' WHERE id = ?",
                            (stamp, existing["id"]))
                updated += 1
                continue
            if existing and job.source_kind == "ats_direct" \
                    and existing["source_kind"] != "ats_direct":
                # Seen first via an aggregator, now found on the employer's own board:
                # adopt the direct link and say where it really comes from.
                cur.execute("UPDATE jobs SET source = ?, source_kind = ? WHERE id = ?",
                            (job.source, job.source_kind, existing["id"]))
            if existing:
                # Refresh everything the source owns - employers edit postings, and a
                # parser fix should reach rows we have already seen. User-owned fields
                # (status, notes, starred, filtered) are deliberately untouched.
                cur.execute(
                    "UPDATE jobs SET last_seen = ?, score = ?, score_reasons = ?, "
                    "matched_skills = ?, missing_skills = ?, title = ?, company = ?, "
                    "url = ?, location = ?, remote = ?, department = ?, "
                    "employment_type = ?, description = ?, salary_min = ?, "
                    "salary_max = ?, salary_currency = ?, salary_display = ?, "
                    "posted_at = ?, closes_at = ?, employment_kind = ?, "
                    "job_family = ?, delisted_at = '' WHERE id = ?",
                    (stamp, job.score, json.dumps(job.score_reasons),
                     json.dumps(job.matched_skills), json.dumps(job.missing_skills),
                     job.title, job.company, job.url, job.location, int(job.remote),
                     job.department, job.employment_type, job.description[:20000],
                     job.salary_min, job.salary_max, job.salary_currency,
                     job.salary_display, job.posted_at, job.closes_at,
                     job.employment_kind, job.job_family, existing["id"]),
                )
                updated += 1
                continue
            cur.execute(
                """INSERT INTO jobs (id, dedupe_key, source, source_kind, company, title, url,
                   location, remote, department, employment_type, description, salary_min,
                   salary_max, salary_currency, salary_display, posted_at, closes_at, score,
                   score_reasons, matched_skills, missing_skills, first_seen, last_seen,
                   employment_kind, job_family)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.dedupe_key, job.source, job.source_kind, job.company, job.title,
                 job.url, job.location, int(job.remote), job.department, job.employment_type,
                 job.description[:20000], job.salary_min, job.salary_max, job.salary_currency,
                 job.salary_display, job.posted_at, job.closes_at, job.score,
                 json.dumps(job.score_reasons), json.dumps(job.matched_skills),
                 json.dumps(job.missing_skills), stamp, stamp,
                 job.employment_kind, job.job_family),
            )
            new_count += 1
        self.conn.commit()
        return new_count, updated

    def set_sponsor(self, job_id: str, info: dict | None) -> None:
        """Record the sponsor-licence lookup so the UI can filter on it."""
        self.conn.execute(
            "UPDATE jobs SET sponsor_name = ?, sponsor_rating = ?, "
            "sponsor_routes = ?, sponsor_conf = ? WHERE id = ?",
            ((info or {}).get("name", ""), (info or {}).get("rating_letter", ""),
             ", ".join((info or {}).get("routes", [])),
             float((info or {}).get("confidence", 0) or 0), job_id),
        )

    def set_filtered(self, job_id: str, filtered: bool, reason: str = "") -> None:
        """Hide or restore a row. Nothing is ever deleted."""
        self.conn.execute(
            "UPDATE jobs SET filtered = ?, filter_reason = ? WHERE id = ?",
            (1 if filtered else 0, reason if filtered else "", job_id),
        )

    def set_status(self, job_id: str, status: str) -> bool:
        if status not in STATUSES:
            return False
        # Stamped so "applied 23 days ago, nothing back" can be surfaced in the app
        # itself, not only in the exported spreadsheet's chase-reminder formula, which
        # only fires on a Date Applied the user typed in by hand.
        self.conn.execute(
            "UPDATE jobs SET status = ?, status_changed_at = ? WHERE id = ?",
            (status, now(), job_id))
        self.conn.commit()
        return True

    def mark_viewed(self, job_id: str) -> None:
        """Stamp the first time a posting's detail panel was opened. Kept as the first
        view, not the latest, so "seen" never flips back and the list can say when."""
        self.conn.execute(
            "UPDATE jobs SET viewed_at = ? WHERE id = ? AND COALESCE(viewed_at, '') = ''",
            (now(), job_id))
        self.conn.commit()

    def set_note(self, job_id: str, note: str) -> None:
        self.conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (note, job_id))
        self.conn.commit()

    def toggle_star(self, job_id: str) -> int:
        cur = self.conn.execute("SELECT starred FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if row is None:
            return 0
        value = 0 if row["starred"] else 1
        self.conn.execute("UPDATE jobs SET starred = ? WHERE id = ?", (value, job_id))
        self.conn.commit()
        return value

    def record_run(self, started: str, found: int, kept: int, new: int, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, found, kept, new, detail) "
            "VALUES (?,?,?,?,?,?)",
            (started, now(), found, kept, new, json.dumps(detail)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------- read
    def query(self, *, status: str | None = None, source: str | None = None,
              company: str | None = None, min_score: float = 0.0, search: str = "",
              starred_only: bool = False, remote_only: bool = False,
              order: str = "score", limit: int = 500, offset: int = 0,
              include_filtered: bool = False, sponsored_only: bool = False,
              employment: list[str] | None = None,
              families: list[str] | None = None,
              include_unclassified: bool = True,
              own_rules: list | None = None,
              salary_disclosed_only: bool = False,
              unviewed_only: bool = False,
              startups: str = "any") -> list[dict]:
        where, params = self._where(
            status=status, source=source, company=company, min_score=min_score,
            search=search, starred_only=starred_only, remote_only=remote_only,
            include_filtered=include_filtered, sponsored_only=sponsored_only,
            employment=employment, families=families,
            include_unclassified=include_unclassified, own_rules=own_rules,
            salary_disclosed_only=salary_disclosed_only, unviewed_only=unviewed_only,
            startups=startups)
        orders = {
            "score": "score DESC, first_seen DESC",
            "date": "COALESCE(posted_at, first_seen) DESC",
            "company": "company COLLATE NOCASE ASC, score DESC",
            "salary": "COALESCE(salary_max, salary_min, 0) DESC, score DESC",
            "new": "first_seen DESC",
            "employment": "employment_kind = '' ASC, employment_kind ASC, score DESC",
            "family": "job_family = '' ASC, job_family ASC, score DESC",
            "closing": "closes_at IS NULL ASC, closes_at ASC",
        }
        # id breaks ties, so "Show more" pages never repeat or skip a posting.
        sql = (f"SELECT * FROM jobs WHERE {where} "
               f"ORDER BY {orders.get(order, orders['score'])}, id LIMIT ? OFFSET ?")
        rows = self.conn.execute(sql, params + [limit, offset]).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self, **filters) -> int:
        """How many postings the same filters match in total, ignoring the page size."""
        where, params = self._where(**filters)
        return self.conn.execute(f"SELECT COUNT(*) c FROM jobs WHERE {where}",
                                 params).fetchone()["c"]

    def facet_counts(self, **filters) -> dict:
        """The number beside every sidebar option, given everything else you've set.

        Each group is counted with all the OTHER filters applied but not its own
        selection - the standard faceted-search rule. Counting a group against its own
        ticks would zero out every option you haven't ticked, so you could never widen
        a choice; ignoring the other filters (what this used to do) printed the
        whole database's numbers no matter what you had narrowed to.
        """
        out: dict = {}
        for group, column, key in (("employment", "employment_kind", "employment"),
                                   ("families", "job_family", "families")):
            where, params = self._where(**{**filters, key: None})
            out[group] = {(r["k"] or "unstated"): r["c"] for r in self.conn.execute(
                f"SELECT {column} k, COUNT(*) c FROM jobs WHERE {where} GROUP BY {column}",
                params)}
        where, params = self._where(**{**filters, "source": None})
        out["sources"] = {r["source"]: r["c"] for r in self.conn.execute(
            f"SELECT source, COUNT(*) c FROM jobs WHERE {where} GROUP BY source "
            "ORDER BY c DESC", params)}
        # A custom rule's number is "how many you'd see with it ticked": every other
        # filter, including your other ticked rules, plus this rule.
        from . import rules as rules_mod
        ticked = list(filters.get("own_rules") or [])
        out["own_rules"] = {}
        for rule in filters.get("all_rules") or []:
            others = [r for r in ticked if r.key != rule.key] + [rule]
            where, params = self._where(**{**filters, "own_rules": others})
            out["own_rules"][rule.key] = self.conn.execute(
                f"SELECT COUNT(*) c FROM jobs WHERE {where}", params).fetchone()["c"]
        return out

    @staticmethod
    def _like(value: str) -> str:
        """A literal substring for LIKE - "100%" or "data_" are text, not wildcards."""
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def _where(self, *, status: str | None = None, source: str | None = None,
               company: str | None = None, min_score: float = 0.0, search: str = "",
               starred_only: bool = False, remote_only: bool = False,
               include_filtered: bool = False, sponsored_only: bool = False,
               employment: list[str] | None = None,
               families: list[str] | None = None,
               include_unclassified: bool = True,
               own_rules: list | None = None,
               salary_disclosed_only: bool = False,
               unviewed_only: bool = False,
               startups: str = "any", all_rules: list | None = None) -> tuple[str, list]:
        """The WHERE clause every listing, total and sidebar count shares, so the
        numbers beside the filters can never disagree with the list they produce."""
        sql = "score >= ?"
        params: list = [min_score]
        if not include_filtered:
            sql += " AND COALESCE(filtered, 0) = 0"
            # Gone postings drop out of the list - unless you starred one or moved it
            # off "new", in which case it stays, flagged, so your history is intact.
            sql += (" AND (COALESCE(delisted_at, '') = '' OR starred = 1 "
                    "OR status <> 'new')")
        if status and status != "all":
            sql += " AND status = ?"
            params.append(status)
        if source and source != "all":
            sql += " AND source = ?"
            params.append(source)
        if company:
            sql += " AND company LIKE ? ESCAPE '\\'"
            params.append(self._like(company))
        if starred_only:
            sql += " AND starred = 1"
        if sponsored_only:
            sql += " AND COALESCE(sponsor_name, '') <> ''"
        for column, wanted in (("employment_kind", employment), ("job_family", families)):
            if not wanted:
                continue
            slots = ",".join("?" * len(wanted))
            # An unclassified posting is unknown, not excluded - let the caller say
            # whether unknowns should still show.
            blank = f" OR COALESCE({column}, '') = ''" if include_unclassified else ""
            sql += f" AND ({column} IN ({slots}){blank})"
            params += list(wanted)
        # The user's own facet rules, already narrowed to the ticked ones by the caller.
        for rule in (own_rules or []):
            from . import rules as rules_mod
            clause, values = rules_mod.sql_clause(rule)
            sql += f" AND {clause}"
            params += values
        if remote_only:
            sql += " AND remote = 1"
        if salary_disclosed_only:
            # ~a quarter of listings omit pay even where the law requires it - a
            # frequently reported complaint. This is the "don't waste my time" filter
            # for it.
            sql += (" AND (salary_min IS NOT NULL OR salary_max IS NOT NULL "
                    "OR COALESCE(salary_display, '') <> '')")
        if unviewed_only:
            sql += " AND COALESCE(viewed_at, '') = ''"
        if startups == "only":
            sql += " AND startup = 1"
        elif startups == "hide":
            sql += " AND COALESCE(startup, 0) = 0"   # unknown employers stay visible
        if search:
            sql += (" AND (title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' "
                    "OR description LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\')")
            params += [self._like(search)] * 4
        return sql, params

    def get(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def mark_delisted(self, run_started: str,
                      complete_boards: dict[str, str | list[str]],
                      fetched_keys: set[str] | None = None) -> dict:
        """Flag postings that are gone: taken down by the employer, or past closing.

        ``complete_boards`` maps company -> source for every company board this run
        read IN FULL. A row from one of those boards that this run did not see has been
        taken down. Boards cut off at the per-company cap are deliberately absent -
        a posting past the cap was not reached, not removed. Aggregator rows are never
        judged this way: they only ever return a page of results, so absence there
        means nothing. A closing date in the past means gone for any source.
        Rows seen again are cleared in upsert(), so a re-listed posting comes back.
        """
        stamp = now()
        removed = 0
        # last_seen only moves for postings that passed the filters and were stored,
        # so a posting still on the board but filtered out this scan (wrong town, too
        # senior, below the minimum score) looked exactly like a removed one. What was
        # FETCHED is the evidence; a posting fetched by any source is not gone.
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS fetched_now (k TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM fetched_now")
        self.conn.executemany("INSERT OR IGNORE INTO fetched_now (k) VALUES (?)",
                              [(k,) for k in (fetched_keys or ())])
        for company, sources in complete_boards.items():
            for source in ([sources] if isinstance(sources, str) else sources):
                cur = self.conn.execute(
                    "UPDATE jobs SET delisted_at = ? WHERE company = ? AND source = ? "
                    "AND last_seen < ? AND COALESCE(delisted_at, '') = '' "
                    "AND dedupe_key NOT IN (SELECT k FROM fetched_now) "
                    "AND id NOT IN (SELECT k FROM fetched_now)",
                    (stamp, company, source, run_started))
                removed += cur.rowcount
        cur = self.conn.execute(
            "UPDATE jobs SET delisted_at = ? WHERE COALESCE(delisted_at, '') = '' "
            "AND COALESCE(closes_at, '') <> '' AND date(closes_at) < date('now')",
            (stamp,))
        self.conn.commit()
        return {"taken_down": removed, "past_closing": cur.rowcount}

    def apply_startup_flags(self, flags: dict[str, bool]) -> int:
        """Stamp each row's employer as startup (1) / not (0) from the company registry.

        NULL means the registry says nothing about that employer - which is most of
        them - and "hide startups" deliberately keeps those rather than guessing.
        """
        changed = 0
        for company, is_startup in flags.items():
            cur = self.conn.execute(
                "UPDATE jobs SET startup = ? WHERE lower(company) = ? "
                "AND COALESCE(startup, -1) <> ?",
                (1 if is_startup else 0, company, 1 if is_startup else 0))
            changed += cur.rowcount
        self.conn.commit()
        return changed

    def company_activity(self, company: str) -> dict:
        """How many roles this exact employer has open, and how many have lingered."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS open_roles, "
            "SUM(CASE WHEN julianday('now') - julianday(COALESCE(NULLIF(posted_at, ''), "
            "first_seen)) >= 45 THEN 1 ELSE 0 END) AS long_open "
            "FROM jobs WHERE company = ? COLLATE NOCASE AND COALESCE(filtered, 0) = 0",
            (company,)).fetchone()
        return {"open_roles": row["open_roles"] or 0, "long_open": row["long_open"] or 0}

    def clear_notes(self) -> int:
        """Blank every note. The one thing in the jobs table the user typed themselves."""
        cur = self.conn.execute(
            "UPDATE jobs SET notes = '' WHERE COALESCE(notes, '') <> ''")
        self.conn.commit()
        return cur.rowcount

    # Same threshold as the exported workbook's own ALERT formula ("Chase - quiet N
    # days"), so the in-app nudge and the spreadsheet agree rather than disagreeing
    # about when 21 days becomes worth mentioning.
    FOLLOW_UP_DAYS = 21

    def follow_ups(self) -> list[dict]:
        """Applications sitting quiet long enough that a nudge, not a status check,
        is what is actually useful - the single most-cited emotional pain point in job
        searching is not knowing when to stop waiting and do something about it."""
        rows = self.conn.execute(
            "SELECT *, julianday('now') - julianday(status_changed_at) AS quiet_days "
            "FROM jobs WHERE status = 'applied' AND status_changed_at <> '' "
            "AND julianday('now') - julianday(status_changed_at) >= ? "
            "ORDER BY status_changed_at ASC", (self.FOLLOW_UP_DAYS,)).fetchall()
        out = []
        for row in rows:
            data = self._row_to_dict(row)
            data["quiet_days"] = int(row["quiet_days"])
            out.append(data)
        return out

    def top_employers(self, families: list[str], kinds: list[str] | None = None,
                      limit: int = 12) -> list[dict]:
        """Employers with the most postings in these fields, from what we have actually
        scraped - not alumni data, which nobody publishes an API for.

        This is the honest version of "which companies hire a lot of people like me":
        real counts from real postings in the local database, not a guess dressed up as
        a statistic. Restricting ``kinds`` to early-career postings (internship,
        placement, graduate_scheme) is what makes it useful for a student rather than
        just listing whoever posts the most jobs overall.
        """
        if not families:
            return []
        slots = ",".join("?" * len(families))
        params: list = list(families)
        kind_clause = ""
        if kinds:
            kind_slots = ",".join("?" * len(kinds))
            kind_clause = f" AND employment_kind IN ({kind_slots})"
            params += list(kinds)
        rows = self.conn.execute(
            f"SELECT company, COUNT(*) c, GROUP_CONCAT(DISTINCT job_family) fams "
            f"FROM jobs WHERE COALESCE(filtered,0)=0 AND job_family IN ({slots})"
            f"{kind_clause} GROUP BY company ORDER BY c DESC, company COLLATE NOCASE "
            f"LIMIT ?", params + [limit]).fetchall()
        return [{"company": r["company"], "postings": r["c"],
                "families": (r["fams"] or "").split(",")} for r in rows]

    def rule_counts(self, own_rules: list) -> dict[str, int]:
        """The number to print next to each custom rule.

        Which number depends on what the rule does, because the useful answer differs.
        A facet's number is how many visible postings ticking it would show. An exclude
        or require rule has already hidden its matches, so counting only visible rows
        would always print zero; those are counted across everything instead, which is
        the "this is hiding N postings" the user wants to see.
        """
        counts: dict[str, int] = {}
        from . import rules as rules_mod
        for rule in own_rules or []:
            clause, values = rules_mod.sql_clause(rule)
            visible = "" if rule.mode in ("exclude", "require")                 else "COALESCE(filtered,0)=0 AND "
            row = self.conn.execute(
                f"SELECT COUNT(*) c FROM jobs WHERE {visible}{clause}", values).fetchone()
            counts[rule.key] = row["c"]
        return counts

    def stats(self) -> dict:
        cur = self.conn.cursor()
        total = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=0").fetchone()["c"]
        hidden = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=1").fetchone()["c"]
        by_status = {r["status"]: r["c"] for r in cur.execute(
            "SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
        by_source = {r["source"]: r["c"] for r in cur.execute(
            "SELECT source, COUNT(*) c FROM jobs GROUP BY source ORDER BY c DESC")}
        by_employment = {(r["employment_kind"] or "unstated"): r["c"] for r in cur.execute(
            "SELECT employment_kind, COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=0 "
            "GROUP BY employment_kind ORDER BY c DESC")}
        by_family = {(r["job_family"] or "unstated"): r["c"] for r in cur.execute(
            "SELECT job_family, COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=0 "
            "GROUP BY job_family ORDER BY c DESC")}
        top_companies = [(r["company"], r["c"]) for r in cur.execute(
            "SELECT company, COUNT(*) c FROM jobs GROUP BY company ORDER BY c DESC LIMIT 12")]
        avg = cur.execute("SELECT AVG(score) a FROM jobs").fetchone()["a"] or 0
        strong = cur.execute("SELECT COUNT(*) c FROM jobs WHERE score >= 60").fetchone()["c"]
        sponsored = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=0 "
            "AND COALESCE(sponsor_name,'') <> ''").fetchone()["c"]
        last_run = cur.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        follow_up_due = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status = 'applied' "
            "AND status_changed_at <> '' AND "
            "julianday('now') - julianday(status_changed_at) >= ?",
            (self.FOLLOW_UP_DAYS,)).fetchone()["c"]
        undisclosed = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE COALESCE(filtered,0)=0 "
            "AND salary_min IS NULL AND salary_max IS NULL "
            "AND COALESCE(salary_display, '') = ''").fetchone()["c"]
        return {
            "total": total, "hidden": hidden,
            "by_status": by_status, "by_source": by_source,
            "top_companies": top_companies, "avg_score": round(avg, 1),
            "strong_matches": strong, "sponsored": sponsored,
            "by_employment": by_employment, "by_family": by_family,
            "last_run": dict(last_run) if last_run else None,
            "follow_up_due": follow_up_due, "salary_undisclosed": undisclosed,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in ("score_reasons", "matched_skills", "missing_skills"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except (json.JSONDecodeError, TypeError):
                data[key] = []
        data["remote"] = bool(data.get("remote"))
        data["starred"] = bool(data.get("starred"))
        data["viewed"] = bool(data.get("viewed_at"))
        data["startup"] = None if data.get("startup") is None else bool(data["startup"])
        data["delisted"] = bool(data.get("delisted_at"))

        # Computed at read time, not stored: it uses the same text every rescore would
        # anyway, so recomputing it on the way out means the 1,919 postings already in
        # the database get the corrected signal immediately, with no migration and no
        # rescore needed to catch up. "remote" stays the raw flag from the source, kept
        # for backward compatibility; "work_mode" is what the UI should actually show.
        from .scoring import work_mode_from_text
        data["work_mode"] = work_mode_from_text(
            data.get("title") or "", data.get("location") or "",
            data.get("description") or "", data["remote"])

        # A listing open a long time is one of the clearest signals of a "ghost job" -
        # Greenhouse's own 2024 data put roughly a fifth of postings on their platform
        # in that category. Flagged rather than hidden: it is a reason to double-check
        # before investing an evening on a cover letter, not a reason to assume it is
        # fake.
        posted = data.get("posted_at") or data.get("first_seen")
        data["long_open"] = False
        if posted:
            try:
                age = (datetime.now(timezone.utc)
                      - datetime.fromisoformat(posted.replace("Z", "+00:00"))).days
                data["long_open"] = age >= 45
            except (ValueError, TypeError):
                pass

        data["salary_disclosed"] = bool(
            data.get("salary_min") or data.get("salary_max")
            or (data.get("salary_display") or "").strip())

        return data
