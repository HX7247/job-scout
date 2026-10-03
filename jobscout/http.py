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
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0}

    # ---------------------------------------------------------------- robots
    def _robots_for(self, url: str):
        host = urlparse(url).netloc
        if host in self._robots:
            return self._robots[host]
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
        host = urlparse(url).netloc
        with self._lock:
            gap = time.time() - self._last_hit.get(host, 0.0)
            wait = self.min_gap - gap
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.25))
            self._last_hit[host] = time.time()

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

        last_error = None
        for attempt in range(retries):
            self._throttle(url)
            try:
                self.stats["requests"] += 1
                resp = self.session.request(
                    method, url, json=json_body, params=params,
                    headers=headers, timeout=timeout or self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Worth another try: rate-limited or the server is having a moment.
                    time.sleep(2 ** attempt + random.uniform(0, 1))
                    last_error = f"HTTP {resp.status_code}"
                    continue
                if 400 <= resp.status_code < 500:
                    # Not worth another try: a 404, a 401, or - the surprisingly common
                    # case for a guessed URL on a real company site - a 403 from a bot
                    # firewall (Cloudflare, Akamai and the like) that will say exactly
                    # the same thing next attempt. Retrying it just adds a pointless
                    # sleep before giving up anyway, so this returns immediately, and
                    # caches the outcome when the caller says the failure mode itself is
                    # informative (a page that does not exist tends to keep not existing).
                    if cache_misses:
                        self._cache_write(cache_path, {"data": None})
                    return None
                resp.raise_for_status()
                data = resp.json() if expect_json else resp.text
                self._cache_write(cache_path, {"data": data})
                return data
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_error = str(exc)[:160]
                time.sleep(1.2 * (attempt + 1))
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
