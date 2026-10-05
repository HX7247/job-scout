"""Generic custom job feeds - the honest, legitimate answer to "what about my
university's career-service site?"

Handshake, the platform most UK/US universities actually run their careers service on,
requires a student login and its ToS explicitly prohibits scraping - this module never
touches it, or any other login-walled portal, at all. What it does instead: the user
supplies URLs of any PUBLIC feed or listing page themselves - their own university's
public careers page, if it happens to publish one; a public RSS feed; any other board not
otherwise covered - and Job Scout fetches and classifies each one. Same
disclosed-and-optional model as an ATS-registry entry the user adds by hand; nothing here
is fetched unless the user gave us the URL first.

Two paths, chosen by what the URL actually serves:
  * RSS/Atom  - parsed deterministically with the stdlib's ElementTree. Works with or
    without an ANTHROPIC_API_KEY, since nothing here is inferred.
  * plain HTML - job-shaped candidate links are collected heuristically, then handed to
    Claude for classification (_classify_candidates), following the same
    evidence-verification idiom as review.py's _claude_review / _verify_evidence: every
    url Claude returns must be an exact substring of the text it was given, or it is
    silently dropped. This path only runs when ANTHROPIC_API_KEY is set - without a key,
    an HTML feed returns [] rather than guessing at what is and is not a job posting.

Every fetch here is of a page the user explicitly configured, and every fetch still goes
through SESSION with check_robots=True - this is arbitrary third-party content, and a
disallow in its robots.txt is respected the same as it would be for anything else.
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import SESSION
from ..models import Job, strip_html
from .aggregators import BaseAggregator

log = logging.getLogger("jobscout.sources.customfeeds")

MODEL = "claude-opus-5"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _looks_like_feed(text: str) -> bool:
    head = text.lstrip("﻿ \t\r\n")[:400].lower()
    return head.startswith("<?xml") or "<rss" in head[:200] or "<feed" in head[:200]


def _parse_feed(text: str, page_url: str) -> list[dict]:
    """RSS 2.0 <item> or Atom <entry> - title/link/description, no LLM needed."""
    try:
        root = ET.fromstring(text.lstrip("﻿"))
    except ET.ParseError as exc:
        log.info("customfeeds: %s did not parse as XML (%s)", page_url, exc)
        return []
    out = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]      # strip any XML namespace prefix
        if tag not in ("item", "entry"):
            continue
        title, link, description, published = "", "", "", ""
        for child in node:
            ctag = child.tag.split("}")[-1]
            if ctag == "title":
                title = (child.text or "").strip()
            elif ctag == "link":
                # RSS: <link>https://...</link>  Atom: <link href="https://.../>
                link = (child.get("href") or child.text or "").strip()
            elif ctag in ("description", "summary", "content") and not description:
                description = (child.text or "").strip()
            elif ctag in ("pubDate", "published", "updated") and not published:
                published = (child.text or "").strip()
        if title and link:
            out.append({"title": title, "url": urljoin(page_url, link),
                        "description": description, "published": published})
    return out


_TITLE_WORD_RE = re.compile(
    r"\b(intern\w*|graduate|analyst|engineer|developer|assistant|coordinator|"
    r"officer|manager|placement|apprentice\w*|trainee|associate|scheme|vacan\w*|"
    r"job|role|position|hiring)\b",
    re.I,
)


def _candidate_links(html: str, page_url: str) -> list[dict]:
    """Heuristic pre-filter, never a final answer on its own: anchors whose visible
    text is short enough to be a job title and either sit inside list/article
    structure or read as job-shaped, so the LLM classifier below is judging a
    short, plausible shortlist rather than every link on the page."""
    soup = BeautifulSoup(html, "lxml")
    seen_hrefs = set()
    candidates = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not (3 <= len(text) <= 140):
            continue
        href = urljoin(page_url, a["href"].strip())
        if href in seen_hrefs or href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        structural = (a.find_parent(["li", "article"]) is not None
                     or a.find_previous(["h1", "h2", "h3", "h4"]) is not None)
        job_shaped = bool(_TITLE_WORD_RE.search(text))
        if not (structural or job_shaped):
            continue
        seen_hrefs.add(href)
        candidates.append({"text": text, "url": href})
        if len(candidates) >= 150:
            break
    return candidates


def _classify_candidates(html_text: str, page_url: str) -> list[dict] | None:
    """Ask Claude which candidate links are genuine job/internship postings. Mirrors
    review.py's _claude_review: no key -> None, forced tool call, never a paraphrase.
    Every returned url is checked against the literal text handed to the model
    (mirroring _verify_evidence) before it is trusted at all."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    candidates = _candidate_links(html_text, page_url)
    if not candidates:
        return None

    chunk = "\n".join(f"TEXT: {c['text']}\nURL: {c['url']}" for c in candidates)

    tool = {
        "name": "job_candidates",
        "description": "Which of these page links are genuine job or internship postings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "company": {"type": "string",
                                       "description": "Leave blank if the page does not say."},
                            "url": {"type": "string",
                                   "description": "Copied character-for-character from "
                                                  "the matching URL: line - never "
                                                  "rewritten, shortened, or invented."},
                        },
                        "required": ["title", "url"],
                    },
                },
            },
            "required": ["jobs"],
        },
    }

    prompt = f"""This is a list of links pulled from one page ({page_url}), each with its
visible text and href. Some are genuine job or internship postings; many will be
navigation, "about us", blog posts, or other non-job links.

{chunk}

Return only the ones that are genuinely individual job or internship postings. For each,
"url" must be copied character-for-character from its "URL:" line above - never
rewritten, shortened, or invented. A url that cannot be found verbatim in the text above
will be discarded before anyone sees it, so copy it exactly."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL, max_tokens=2000,
            tools=[tool], tool_choice={"type": "tool", "name": "job_candidates"},
            messages=[{"role": "user", "content": prompt}])
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                jobs = block.input.get("jobs") or []
                needle_source = _normalise(chunk)
                verified = []
                for job in jobs:
                    url = (job.get("url") or "").strip()
                    if url and _normalise(url) in needle_source:
                        verified.append(job)
                    else:
                        log.info("customfeeds: dropped an unverifiable url from %s",
                                 page_url)
                return verified
    except Exception as exc:
        log.warning("Claude unavailable for custom-feed classification: %s", exc)
    return None


class CustomFeedSource(BaseAggregator):
    """User-supplied public feeds/pages. Opt-in: does nothing until the user configures
    at least one feed_urls entry, which is correct, expected behaviour, not a bug."""

    name = "custom_feeds"

    uses_query = False
    kind = "board"
    needs_key = False

    def fetch(self, query: str = "", location: str = "", max_results: int = 200,
              home=None, feed_urls: list[str] | None = None, **kwargs) -> list[Job]:
        feed_urls = feed_urls or []
        jobs: list[Job] = []
        for url in feed_urls:
            try:
                text = SESSION.get_text(url, check_robots=True)
                if not text:
                    log.info("customfeeds: %s unreachable or blocked by robots.txt - "
                             "skipping", url)
                    continue
                if _looks_like_feed(text):
                    for entry in _parse_feed(text, url):
                        jobs.append(Job(
                            source=self.name, source_kind=self.kind,
                            company="", title=entry["title"], url=entry["url"],
                            description=strip_html(entry.get("description", "")),
                            raw={"feed_url": url},
                        ))
                else:
                    classified = _classify_candidates(text, url)
                    for item in (classified or []):
                        job_url = (item.get("url") or "").strip()
                        title = (item.get("title") or "").strip()
                        if not job_url or not title:
                            continue        # never invent a URL or a title
                        jobs.append(Job(
                            source=self.name, source_kind=self.kind,
                            company=(item.get("company") or "").strip(),
                            title=title, url=job_url,
                            raw={"feed_url": url},
                        ))
            except Exception as exc:
                log.warning("customfeeds: %s failed (%s)", url, exc)
                continue
            if len(jobs) >= max_results:
                break
        return jobs[:max_results]
