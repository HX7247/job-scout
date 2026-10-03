"""Local accounts, so your profile and criteria survive a restart.

WHAT A LOGIN IS HERE
--------------------
This is a program running on your own machine, not a service. So the login is a lock on
the local data, not a cloud account, and it is built to hold as little as possible:

  * a username you invent - not an email address, because we do not want your email
  * a scrypt hash of your password, with a per-account random salt
  * a created date

That is the whole accounts table. There is no email, no security question, no recovery
address, and therefore no password reset by mail: if you forget it, the data for that
account is unreadable and you start a new one. That is the honest trade for not holding
a way to contact you.

Each account owns a directory holding its own search criteria and derived profile:

    data/accounts/<account-id>/config.yaml     titles, keywords, filters
    data/accounts/<account-id>/profile.json    education, skills, region - see privacy.py

Job postings stay in the shared database, because they are employers' public adverts and
identical for everyone. Per-account state (status, notes, stars) lives in the same table:
single-user by design, and pretending otherwise would be security theatre.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("jobscout.accounts")

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "accounts.db"
ACCOUNTS_DIR = ROOT / "data" / "accounts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    salt       TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen  TEXT
);
"""

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
# Anything that looks like an email is refused rather than stored - see the module note.
EMAILISH = re.compile(r"@")

# scrypt parameters. n=2**15 costs ~50ms and 32MB per attempt on a normal laptop, which
# is slow enough to make an offline guessing attack on the file expensive.
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 64, "maxmem": 64 * 1024 * 1024}


class AccountError(ValueError):
    """Something the user can fix, phrased for them rather than for a log."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                          **SCRYPT).hex()


class Accounts:
    def __init__(self, path: str | Path = DB_PATH, accounts_dir: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Per-account folders live beside the database that owns them. They used to go
        # to the global ACCOUNTS_DIR whatever database was passed in, so every Accounts
        # built on a throwaway test database left orphaned folders - with copies of the
        # shared config inside - in the real data/accounts/. For the default database
        # this resolves to exactly ACCOUNTS_DIR, as before.
        self.accounts_dir = Path(accounts_dir) if accounts_dir else self.path.parent / "accounts"
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

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

    # ------------------------------------------------------------------ accounts
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]

    def create(self, username: str, password: str) -> dict:
        username = (username or "").strip()
        if EMAILISH.search(username):
            raise AccountError(
                "Pick a username rather than an email address - this app does not keep "
                "your email, so an address here would be the only personal data in the "
                "whole install.")
        if not USERNAME_RE.match(username):
            raise AccountError("Username: 3-32 characters, letters, numbers, dot, dash "
                               "or underscore.")
        if len(password or "") < 8:
            raise AccountError("Password needs at least 8 characters. There is no reset "
                               "by email, so use something you will remember.")
        if self.conn.execute("SELECT 1 FROM accounts WHERE username = ? COLLATE NOCASE",
                             (username,)).fetchone():
            raise AccountError("That username is taken on this machine.")

        salt = secrets.token_hex(16)
        account_id = secrets.token_hex(8)
        self.conn.execute(
            "INSERT INTO accounts (id, username, salt, pw_hash, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, username, salt, _hash(password, salt), _now(), _now()))
        self.conn.commit()
        self.directory(account_id).mkdir(parents=True, exist_ok=True)
        log.info("created account %s", username)
        return {"id": account_id, "username": username}

    def verify(self, username: str, password: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE username = ? COLLATE NOCASE",
            ((username or "").strip(),)).fetchone()
        if not row:
            return None
        # compare_digest, not ==, so a wrong password cannot be narrowed down by timing.
        if not secrets.compare_digest(_hash(password or "", row["salt"]), row["pw_hash"]):
            return None
        self.conn.execute("UPDATE accounts SET last_seen = ? WHERE id = ?",
                          (_now(), row["id"]))
        self.conn.commit()
        return {"id": row["id"], "username": row["username"]}

    def change_password(self, account_id: str, old: str, new: str) -> None:
        row = self.conn.execute("SELECT * FROM accounts WHERE id = ?",
                                (account_id,)).fetchone()
        if not row or not secrets.compare_digest(_hash(old or "", row["salt"]),
                                                 row["pw_hash"]):
            raise AccountError("Current password does not match.")
        if len(new or "") < 8:
            raise AccountError("New password needs at least 8 characters.")
        salt = secrets.token_hex(16)
        self.conn.execute("UPDATE accounts SET salt = ?, pw_hash = ? WHERE id = ?",
                          (salt, _hash(new, salt), account_id))
        self.conn.commit()

    def get(self, account_id) -> dict | None:
        """The account behind a session cookie, or None.

        A cookie outlives the account it names - deleted in another window, or a
        database replaced underneath it - and a cookie is user-controlled input either
        way. So this validates the shape before it reaches sqlite and treats anything
        unresolvable as simply signed out.
        """
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9a-f]{16}",
                                                               account_id):
            return None
        row = self.conn.execute(
            "SELECT id, username, created_at, last_seen FROM accounts WHERE id = ?",
            (account_id,)).fetchone()
        return dict(row) if row else None

    def usernames(self) -> list[str]:
        """For the sign-in screen's 'who is using this machine' hint."""
        return [r["username"] for r in self.conn.execute(
            "SELECT username FROM accounts ORDER BY last_seen DESC")]

    def delete(self, account_id: str, password: str) -> None:
        """Remove the account and everything derived from its LinkedIn. Irreversible."""
        if not self.get(account_id):
            raise AccountError("No such account.")
        row = self.conn.execute("SELECT * FROM accounts WHERE id = ?",
                                (account_id,)).fetchone()
        if not secrets.compare_digest(_hash(password or "", row["salt"]), row["pw_hash"]):
            raise AccountError("Password does not match, so nothing was deleted.")
        import shutil
        directory = self.directory(account_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.conn.commit()
        log.info("deleted account %s and its profile", account_id)

    # ------------------------------------------------------------- per-account files
    def directory(self, account_id: str) -> Path:
        return self.accounts_dir / account_id

    def config_path(self, account_id: str) -> Path:
        return self.directory(account_id) / "config.yaml"

    def profile_path(self, account_id: str) -> Path:
        return self.directory(account_id) / "profile.json"

    def save_profile(self, account_id: str, profile: dict) -> None:
        """Write the derived profile, allowlist-filtered on the way in.

        Filtered here as well as at the point of derivation. Two chances to drop an
        identifier is the right number when the cost of missing one is permanent.
        """
        from . import privacy
        clean = privacy.clean_profile_dict(profile or {})
        stage = (profile or {}).get("career_stage", "")
        if stage:
            clean["career_stage"] = stage
        path = self.profile_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean, indent=2, ensure_ascii=False), "utf-8")
        self._restrict(path)

    def load_profile(self, account_id: str) -> dict | None:
        path = self.profile_path(account_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("unreadable profile for %s: %s", account_id, exc)
            return None

    def forget_profile(self, account_id: str) -> bool:
        """Drop the derived profile but keep the account and its search criteria."""
        path = self.profile_path(account_id)
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def _restrict(path: Path) -> None:
        """Owner-only where the platform supports it. Best effort by design.

        On Windows the POSIX bits are ignored; the file still sits under the user's
        own profile directory, which is where the real protection comes from.
        """
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# ------------------------------------------------------------------ session secret
def session_secret(path: Path | None = None) -> bytes:
    """A stable Flask session key, so signing in survives a restart.

    Generated once and kept in data/, not derived from anything about the user.
    """
    path = path or (ROOT / "data" / "session.key")
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    Accounts._restrict(path)
    return key
