"""Reddit "who is hiring"-style subreddits, via Reddit's official OAuth2 API.

Reddit closed off unauthenticated public JSON access to reddit.com in 2023 (an
unauthenticated request to e.g. ``https://www.reddit.com/r/internships/new.json``
now comes back as a 403 bot-block page even with a browser-like User-Agent), so
this adapter goes through the real script-app flow instead: an app-only
``client_credentials`` token from ``https://www.reddit.com/api/v1/access_token``,
then bearer-authenticated reads against ``https://oauth.reddit.com/...``.

The interesting problem here isn't the HTTP - it's that a subreddit's "new" feed
is mostly questions, rants and results posts, not actual openings. Same shape of
problem ``HNHiringSource`` (in aggregators.py) solves with naive string-splitting
for Hacker News threads; here an LLM agent does the classification+extraction,
with the same anti-hallucination discipline ``review.py`` uses for its
evidence-cited CV notes: nothing the model claims about company/role is trusted
unless it can be found, as a real substring, in the post it supposedly came from.
"""
from __future__ import annotations

import logging
import os
import re
import time

from ..http import SESSION
from ..models import Job, parse_date, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.reddit")

MODEL = "claude-opus-5"

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

DEFAULT_SUBREDDITS = ("internships", "csMajors", "cscareerquestions")


class RedditSource(BaseAggregator):
    """Newest posts from hiring-adjacent subreddits, LLM-filtered down to real postings."""

    name = "reddit"

    uses_query = False
    kind = "board"
    needs_key = True

    # Rule-based fallback used only when there's no ANTHROPIC_API_KEY - deliberately
    # tight, mirroring how conservative HNHiringSource's own fallback logic is.
    # Deliberately NOT a bare "hiring"/"internship" word match: r/internships has
    # that word in nearly every title regardless of whether it's a real posting,
    # so only the bracket-tag convention or an explicit hiring phrase counts.
    _TITLE_HIRING_RE = re.compile(
        r"\[\s*(?:hiring|internship|intern)\s*\]"
        r"|\b(?:now|we'?re|we\s+are|company\s+is)\s+hiring\b"
        r"|\breferrals?\s+(?:available|offer(?:ed)?)\b", re.I)
    _FLAIR_HIRING_RE = re.compile(r"hiring|position|opening", re.I)
    _WORD_RE = re.compile(r"[a-z0-9]+")

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def available(self) -> bool:
        return bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"))

    # ------------------------------------------------------------------ auth
    def _get_token(self) -> str | None:
        """App-only client_credentials token, cached on the instance until near expiry."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        client_id = os.environ.get("REDDIT_CLIENT_ID", "")
        client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        if not (client_id and client_secret):
            return None
        try:
            # SESSION.request()/get_json()/post_json() only ever send a JSON body,
            # but Reddit's token endpoint wants HTTP Basic Auth + a form-encoded
            # grant_type body - a shape the shared wrapper doesn't support. Rather
            # than open a separate requests.Session(), this reuses the *same*
            # shared session (identical User-Agent, connection pool) and its
            # per-host throttle, just below the JSON-only request() layer.
            SESSION._throttle(TOKEN_URL)
            resp = SESSION.session.post(
                TOKEN_URL,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                timeout=SESSION.timeout,
            )
            if resp.status_code >= 400:
                log.warning("Reddit token request failed: HTTP %s %s",
                            resp.status_code, resp.text[:200])
                return None
            payload = resp.json()
        except Exception as exc:
            log.warning("Reddit token request failed: %s", exc)
            return None
        token = payload.get("access_token")
        if not token:
            return None
        self._token = token
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return token

    # ----------------------------------------------------------------- fetch
    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
              **kwargs) -> list[Job]:
        if not self.available():
            return []
        token = self._get_token()
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}"}

        jobs: list[Job] = []
        for sub in subreddits:
            try:
                data = SESSION.get_json(
                    f"{API_BASE}/r/{sub}/new",
                    params={"limit": 75},
                    headers=headers, check_robots=False,
                )
                if not data:
                    continue
                children = (data.get("data") or {}).get("children") or []
                for child in children:
                    post = child.get("data") or {}
                    job = self._job_from_post(post, sub)
                    if job:
                        jobs.append(job)
                    if len(jobs) >= max_results:
                        return jobs
            except Exception as exc:
                log.warning("Reddit fetch failed for r/%s: %s", sub, exc)
                continue
        return jobs[:max_results]

    # ------------------------------------------------------------- one post
    def _job_from_post(self, post: dict, subreddit: str) -> Job | None:
        title = post.get("title", "") or ""
        selftext = post.get("selftext", "") or ""
        permalink = post.get("permalink", "") or ""
        # The URL is always built from the real permalink the API gave us - the
        # model is never asked for, or trusted with, a URL.
        url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
        if not url:
            return None

        if os.environ.get("ANTHROPIC_API_KEY"):
            extracted = self._extract_posting(title, selftext, subreddit)
        else:
            extracted = self._fallback_extract(title, post)
        if not extracted or not extracted.get("is_posting"):
            return None

        return Job(
            source=self.name, source_kind=self.kind,
            company=extracted.get("company") or "See posting",
            title=extracted.get("role") or title,
            url=url,
            external_id=str(post.get("id", "")),
            location=extracted.get("location") or "See posting",
            description=strip_html(selftext)[:4000],
            posted_at=parse_date(post.get("created_utc")),
            raw={"subreddit": subreddit, "permalink": permalink},
        )

    # ------------------------------------------------------------ LLM agent
    def _extract_posting(self, title: str, selftext: str, subreddit: str) -> dict | None:
        """Ask Claude whether this post is a genuine opening, following the same
        graceful-fallback idiom as review.py's _claude_review: no key or no SDK
        just means None, and the caller (fetch, via _job_from_post) treats that
        as "skip this post" rather than raising."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            import anthropic
        except ImportError:
            return None

        tool = {
            "name": "posting_extraction",
            "description": "Decide whether a Reddit post is someone sharing a real "
                           "internship/job opening, and if so extract company/role/location.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "is_posting": {
                        "type": "boolean",
                        "description": "True only if this post itself shares a specific "
                                       "internship/job opening.",
                    },
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "location": {"type": "string"},
                    "reasoning_note": {"type": "string"},
                },
                "required": ["is_posting", "company", "role", "location", "reasoning_note"],
            },
        }

        prompt = f"""SUBREDDIT: r/{subreddit}
TITLE: {title}
BODY:
{selftext[:3000]}

Only set is_posting=true if this post is someone actually sharing a specific
internship/job opening (a company hiring, a referral offer, a "here's an
opening at X" post) - not a question, a rant, a results/rejection post, or a
generic discussion. If is_posting is true, company and role MUST be genuinely
stated or clearly inferable from the post text itself - never invent a
plausible-sounding company or role that isn't actually in the text. If you
cannot tell what company or role this is, set is_posting=false instead of
guessing."""

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=MODEL, max_tokens=500,
                tools=[tool], tool_choice={"type": "tool", "name": "posting_extraction"},
                messages=[{"role": "user", "content": prompt}])
            result = None
            for block in response.content:
                if getattr(block, "type", "") == "tool_use":
                    result = block.input
                    break
            if result is None:
                return None
        except Exception as exc:
            log.warning("Claude unavailable for Reddit posting extraction: %s", exc)
            return None

        if not result.get("is_posting"):
            return {"is_posting": False}
        if not self._grounded(result, title, selftext):
            log.info("Dropping Reddit posting from r/%s - company/role not found "
                      "in source text (title=%r)", subreddit, title[:80])
            return {"is_posting": False}
        return result

    def _grounded(self, result: dict, title: str, selftext: str) -> bool:
        """Cheap anti-hallucination check, same spirit as review.py's _verify_evidence:
        require at least one significant word of each of company/role to actually
        appear (case-insensitively, word by word) in the real post text - never
        the full phrase, since the model may reasonably normalise capitalisation
        or whitespace, but a genuinely invented company/role won't have any word
        overlap with the source at all."""
        haystack = f"{title}\n{selftext}".lower()
        for field_name in ("company", "role"):
            value = str(result.get(field_name, "")).strip()
            if not value:
                return False
            words = [w for w in self._WORD_RE.findall(value.lower()) if len(w) >= 3]
            if not words:
                return False
            if not any(w in haystack for w in words):
                return False
        return True

    # -------------------------------------------------------- rule fallback
    def _fallback_extract(self, title: str, post: dict) -> dict:
        """No ANTHROPIC_API_KEY: conservative rule-based keep/drop, honesty-matched
        to HNHiringSource's own fallback for the same ambiguity on Hacker News -
        a post is kept only on a tight signal, and the company is never guessed
        from the title, just marked "See posting" the same way."""
        flair = post.get("link_flair_text") or ""
        flaired_hiring = bool(flair) and bool(self._FLAIR_HIRING_RE.search(flair))
        title_matches = bool(self._TITLE_HIRING_RE.search(title))
        if not (flaired_hiring or title_matches):
            return {"is_posting": False}
        return {
            "is_posting": True,
            "company": "See posting",
            "role": title[:180],
            "location": "See posting",
            "reasoning_note": "rule-based fallback (no ANTHROPIC_API_KEY): "
                              f"{'flair' if flaired_hiring else 'title'} matched a hiring pattern",
        }
