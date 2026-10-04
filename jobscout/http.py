"""Polite HTTP layer: identifies itself, honours robots.txt, rate-limits per host, caches."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
import urllib.robotparser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger("jobscout.http")

USER_AGENT = (
    "JobScout/0.1 (personal job-search assistant; respects robots.txt; "
    "contact via the operator of this machine)"
)
CACHE_DIR = Path(os.environ.get("JOBSCOUT_CACHE", Path(__file__).resolve().parent.parent / "data" / "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class PoliteSession:
    """One shared session. Serialises requests per host with a minimum gap between them."""

    def __init__(self, min_gap: float = 1.0, timeout: int = 25, cache_ttl: int = 1800,
                 obey_robots: bool = True, max_cache_bytes: int = 2 * 1024 * 1024):
        self.min_gap = min_gap
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_cache_bytes = max_cache_bytes
        self.obey_robots = obey_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        # Circuit breaker, per host: after FAIL_LIMIT failures in a row the host is
        # left alone until _open_until passes, so one site that is down (or asking us
        # to back off) costs a few seconds instead of retries x timeouts x boards.
        self._fail_streak: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._host_errors: dict[str, str] = {}
        self._gaps: dict[str, float] = {}       # a host's gap, once a 429 has widened it
        self.session = requests.Session()
        # The default pool keeps 10 connections per host; the scrape runs more
        # workers than that against a few big hosts (Workday, Greenhouse).
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0,
                      "rate_limited": 0, "breaker_skips": 0}

    FAIL_LIMIT = 4               # consecutive failures before a host is rested
    REST_SECONDS = 600           # how long a tripped host is left alone
    MAX_RETRY_AFTER = 60.0       # longer than this and the host is rested instead
    # Public job-board APIs built to be read by programs, each serving hundreds of the
    # registry's boards from one host. At the default gap the 179 Ashby boards alone
    # took three minutes. A 429 doubles the gap for the rest of the scan.
    HOST_GAPS = {"api.ashbyhq.com": 0.4, "boards-api.greenhouse.io": 0.4,
                 "api.lever.co": 0.4}

    # ---------------------------------------------------------------- robots
    def _robots_for(self, url: str):
        host = urlparse(url).netloc
        if host in self._robots:
            return self._robots[host]
        # One fetch per host, even when eight workers ask for it at the same moment.
        with self._lock:
            host_lock = self._robots_locks.setdefault(host, threading.Lock())
        with host_lock:
            if host in self._robots:
                return self._robots[host]
            return self._fetch_robots(url, host)

    def _fetch_robots(self, url: str, host: str):
        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=10)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                parser = None            # no robots served == no restrictions published
        except requests.RequestException:
            parser = None
        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        ok = parser.can_fetch(USER_AGENT, url)
        if not ok:
            self.stats["robots_blocked"] += 1
            log.warning("robots.txt disallows %s - skipping", url)
        return ok

    # ---------------------------------------------------------------- limits
    def _throttle(self, url: str) -> None:
        """Wait for this host's next free slot, at least min_gap after the last one.

        The slot is booked under the lock but slept for outside it. Sleeping while
        holding the lock - as this used to - made every thread wait on whichever host
        was busiest, so eight workers fetching eight different hosts went one at a time.
        """
        host = urlparse(url).netloc
        with self._lock:
            now = time.time()
            slot = max(now, self._last_hit.get(host, 0.0) + self._gap(host))
            self._last_hit[host] = slot + random.uniform(0, 0.25)
        if slot > now:
            time.sleep(slot - now)

    def _gap(self, host: str) -> float:
        return self._gaps.get(host) or self.HOST_GAPS.get(host) or self.min_gap

    def _hold_off(self, host: str, seconds: float) -> None:
        """Push this host's next slot back - what a 429's Retry-After asks for."""
        with self._lock:
            self._last_hit[host] = max(self._last_hit.get(host, 0.0),
                                       time.time() + seconds - self._gap(host))

    def _slow_down(self, host: str) -> None:
        """A 429 means the gap is too short for this host: double it, up to 5s."""
        with self._lock:
            self._gaps[host] = min(5.0, max(self.min_gap, 2 * self._gap(host)))

    # ---------------------------------------------------------------- health
    def _breaker_open(self, host: str) -> bool:
        return self._open_until.get(host, 0.0) > time.time()

    def _failed(self, host: str, why: str, rest: float | None = None) -> None:
        with self._lock:
            streak = self._fail_streak.get(host, 0) + 1
            self._fail_streak[host] = streak
            self._host_errors[host] = why
            if rest is not None or streak >= self.FAIL_LIMIT:
                rest = rest or self.REST_SECONDS
                self._open_until[host] = time.time() + rest
                log.warning("%s failing (%s) - leaving it alone for %ds", host, why, rest)

    def _succeeded(self, host: str) -> None:
        if self._fail_streak.get(host):
            with self._lock:
                self._fail_streak[host] = 0
                self._open_until.pop(host, None)
                self._host_errors.pop(host, None)

    def health(self) -> dict:
        """Request counters plus the hosts failing right now, for the scan summary."""
        now = time.time()
        with self._lock:
            failing = {host: {"error": why,
                              "streak": self._fail_streak.get(host, 0),
                              "resting_for": max(0, int(self._open_until.get(host, 0) - now))}
                       for host, why in self._host_errors.items()}
        return {"stats": dict(self.stats), "failing_hosts": failing}

    def reset_health(self) -> None:
        """Start a scan with a clean slate: a host that was down last time gets a try."""
        with self._lock:
            self._fail_streak.clear()
            self._open_until.clear()
            self._host_errors.clear()
            self._gaps.clear()
            for key in self.stats:
                self.stats[key] = 0

    @staticmethod
    def _retry_after(resp) -> float | None:
        raw = (resp.headers.get("Retry-After") or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return float(raw)
        try:
            return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- cache
    def _cache_path(self, method: str, url: str, body: str) -> Path:
        key = hashlib.sha1(f"{method}{url}{body}".encode()).hexdigest()
        return CACHE_DIR / f"{key}.json"

    def _cache_read(self, path: Path):
        if self.cache_ttl <= 0 or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            self.stats["cache_hits"] += 1
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _cache_write(self, path: Path, payload) -> None:
        if self.cache_ttl <= 0:
            return
        try:
            body = json.dumps(payload)
        except (TypeError, ValueError):
            return
        # Bulk downloads (the 10MB sponsor register, say) have their own on-disk
        # home; duplicating them here grew the cache to 120MB in a few runs.
        if len(body) > self.max_cache_bytes:
            return
        try:
            path.write_text(body, "utf-8")
        except OSError:
            pass

    def prune_cache(self, max_total_bytes: int = 200 * 1024 * 1024) -> int:
        """Drop expired entries, then the oldest files until under the size cap."""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p)
                     for p in CACHE_DIR.glob("*.json")]
        except OSError:
            return 0
        removed = 0
        cutoff = time.time() - max(self.cache_ttl, 1)
        for mtime, _size, path in list(files):
            if mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                    files = [f for f in files if f[2] != path]
                except OSError:
                    pass
        total = sum(size for _m, size, _p in files)
        for mtime, size, path in sorted(files):
            if total <= max_total_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
        return removed

    # ---------------------------------------------------------------- fetch
    def request(self, method: str, url: str, *, json_body=None, params=None,
                headers=None, retries: int = 3, expect_json: bool = True,
                check_robots: bool = True, timeout: float | None = None,
                cache_misses: bool = False):
        """Fetch, with the shared cache, robots check and per-host throttle.

        ``timeout`` overrides ``self.timeout`` for callers that would rather fail fast
        than wait the full default - a page-existence probe, say, where a slow host is
        as good as no answer. ``cache_misses`` additionally caches a 404 as a cached
        ``None``, which matters for any caller that tries several guessed URLs per
        lookup: without it, a page that genuinely does not exist is re-fetched from
        scratch, throttle and all, on every single call, forever.
        """
        if check_robots and not self.allowed(url):
            return None
        body = json.dumps(json_body or {}, sort_keys=True) + json.dumps(params or {}, sort_keys=True)
        cache_path = self._cache_path(method, url, body)
        cached = self._cache_read(cache_path)
        if cached is not None:
            return cached["data"]

        host = urlparse(url).netloc
        last_error = None
        for attempt in range(retries):
            if self._breaker_open(host):       # possibly tripped by another thread
                self.stats["breaker_skips"] += 1
                return None
            self._throttle(url)
            try:
                self.stats["requests"] += 1
                resp = self.session.request(
                    method, url, json=json_body, params=params,
                    headers=headers, timeout=timeout or self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Worth another try: rate-limited or the server is having a moment.
                    # A Retry-After is honoured, for every thread on this host; one
                    # asking for longer than a scan should wait rests the host instead.
                    last_error = f"HTTP {resp.status_code}"
                    if resp.status_code == 429:
                        self.stats["rate_limited"] += 1
                        self._slow_down(host)
                    wait = self._retry_after(resp)
                    if wait is not None and wait > self.MAX_RETRY_AFTER:
                        self._failed(host, f"{last_error}, asked to wait {int(wait)}s",
                                     rest=wait)
                        break
                    if wait is None:
                        wait = 2 ** attempt + random.uniform(0, 1)
                    self._hold_off(host, wait)
                    continue
                if 400 <= resp.status_code < 500:
                    # Not worth another try: a 404, a 401, or - the surprisingly common
                    # case for a guessed URL on a real company site - a 403 from a bot
                    # firewall (Cloudflare, Akamai and the like) that will say exactly
                    # the same thing next attempt. Retrying it just adds a pointless
                    # sleep before giving up anyway, so this returns immediately, and
                    # caches the outcome when the caller says the failure mode itself is
                    # informative (a page that does not exist tends to keep not existing).
                    # The host did answer, so as far as the breaker goes it is healthy.
                    self._succeeded(host)
                    if cache_misses:
                        self._cache_write(cache_path, {"data": None})
                    return None
                resp.raise_for_status()
                data = resp.json() if expect_json else resp.text
                self._succeeded(host)
                self._cache_write(cache_path, {"data": data})
                return data
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_error = str(exc)[:160]
                if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                    # Unreachable or hanging: counted per attempt, so a dead host
                    # trips the breaker within one board instead of after dozens.
                    self._failed(host, type(exc).__name__)
                time.sleep(1.2 * (attempt + 1))
        else:
            if last_error and last_error.startswith("HTTP"):
                self._failed(host, last_error)
        self.stats["errors"] += 1
        log.debug("giving up on %s (%s)", url, last_error)
        return None

    def get_json(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post_json(self, url: str, json_body: dict, **kwargs):
        return self.request("POST", url, json_body=json_body, **kwargs)

    def get_text(self, url: str, **kwargs):
        return self.request("GET", url, expect_json=False, **kwargs)


SESSION = PoliteSession()
