"""Applicant-tracking-system adapters.

These hit the same public JSON endpoints a company's own careers page calls to render
itself, so postings arrive at source - usually before aggregators index them.
"""
from __future__ import annotations

import logging
import re
import threading

from .. import geo
from ..http import SESSION
from ..models import Job, strip_html, parse_date

log = logging.getLogger("jobscout.sources.ats")

# Set by the pipeline so salary parsing knows which currency to prefer when a
# posting quotes several. None is fine - geo falls back to whatever is quoted.
HOME_COUNTRY = None

# How many postings to take from any one employer. Workday's page size is fixed at
# 20 by the server, so this is 20 requests per large employer at the default.
MAX_PER_COMPANY = 400


# Boards whose most recent fetch did NOT reach the real end of the listing - a page
# failed part-way, or the per-company cap was hit - keyed (adapter, slug). A paging
# adapter that just stopped on a failed page used to hand back a partial list that
# looked complete: AstraZeneca's 1,033 postings came back as 23, and the pipeline then
# judged the other 1,010 "taken down". The pipeline never judges removals from these.
_INCOMPLETE: set[tuple[str, str]] = set()
_INCOMPLETE_LOCK = threading.Lock()


def _record_completeness(adapter: str, slug: str, complete: bool) -> None:
    with _INCOMPLETE_LOCK:
        if complete:
            _INCOMPLETE.discard((adapter, slug))
        else:
            _INCOMPLETE.add((adapter, slug))


def fetch_was_complete(adapter: str, slug: str) -> bool:
    """False if this board's last fetch stopped short of the real end."""
    with _INCOMPLETE_LOCK:
        return (adapter, slug) not in _INCOMPLETE


def set_home_country(country) -> None:
    global HOME_COUNTRY
    HOME_COUNTRY = country


def _salary_from_text(text: str) -> tuple[float | None, float | None, str]:
    """Pull a pay range out of free text, in whatever currency it is quoted."""
    return geo.parse_salary(text, HOME_COUNTRY)


class BaseATS:
    name = "ats"
    kind = "ats_direct"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        raise NotImplementedError


class GreenhouseSource(BaseATS):
    name = "greenhouse"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        data = SESSION.get_json(url)
        if not data or "jobs" not in data:
            return []
        jobs = []
        for item in data["jobs"]:
            desc = strip_html(item.get("content", ""))
            lo, hi, cur = _salary_from_text(desc)
            offices = item.get("offices") or []
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or slug.replace("-", " ").title(),
                title=item.get("title", ""),
                url=item.get("absolute_url", ""),
                external_id=str(item.get("id", "")),
                location=(item.get("location") or {}).get("name", "")
                         or ", ".join(o.get("name", "") for o in offices),
                description=desc,
                posted_at=parse_date(item.get("updated_at") or item.get("first_published")),
                department=", ".join(d.get("name", "") for d in (item.get("departments") or [])),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=item,
            ))
        return jobs


class LeverSource(BaseATS):
    name = "lever"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        data = SESSION.get_json(url)
        if not isinstance(data, list):
            return []
        jobs = []
        for item in data:
            cats = item.get("categories") or {}
            desc = strip_html(item.get("descriptionPlain") or item.get("description", ""))
            extra = " ".join(strip_html(l.get("text", "")) for l in (item.get("lists") or []))
            body = f"{desc}\n{extra}".strip()
            lo, hi, cur = _salary_from_text(body)
            rng = item.get("salaryRange") or {}
            if rng:
                lo = rng.get("min") or lo
                hi = rng.get("max") or hi
                cur = rng.get("currency") or cur
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or slug.replace("-", " ").title(),
                title=item.get("text", ""),
                url=item.get("hostedUrl", ""),
                external_id=str(item.get("id", "")),
                location=cats.get("location", ""),
                department=cats.get("team", "") or cats.get("department", ""),
                employment_type=cats.get("commitment", ""),
                description=body,
                posted_at=parse_date(item.get("createdAt")),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=item,
            ))
        return jobs


class AshbySource(BaseATS):
    name = "ashby"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        data = SESSION.get_json(url)
        if not data or "jobs" not in data:
            return []
        jobs = []
        for item in data["jobs"]:
            desc = strip_html(item.get("descriptionHtml") or item.get("descriptionPlain", ""))
            comp = item.get("compensation") or {}
            summary = comp.get("compensationTierSummary") or ""
            lo, hi, cur = _salary_from_text(summary or desc)
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or (data.get("name") or slug).title(),
                title=item.get("title", ""),
                url=item.get("jobUrl", "") or item.get("applyUrl", ""),
                external_id=str(item.get("id", "")),
                location=item.get("location", ""),
                remote=bool(item.get("isRemote")),
                department=item.get("department", "") or item.get("team", ""),
                employment_type=item.get("employmentType", ""),
                description=desc,
                posted_at=parse_date(item.get("publishedAt")),
                salary_raw=summary,
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=item,
            ))
        return jobs


class SmartRecruitersSource(BaseATS):
    name = "smartrecruiters"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        jobs, offset = [], 0
        reached_end = False
        while offset < MAX_PER_COMPANY:
            url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                   f"?limit=100&offset={offset}")
            data = SESSION.get_json(url)
            if data is None:
                break                          # a failed page - the end was NOT reached
            if not data.get("content"):
                reached_end = True
                break
            for item in data["content"]:
                loc = item.get("location") or {}
                where = ", ".join(x for x in [loc.get("city"), loc.get("region"),
                                              (loc.get("country") or "").upper()] if x)
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=company or (item.get("company") or {}).get("name", slug),
                    title=item.get("name", ""),
                    url=item.get("applyUrl", "")
                        or f"https://jobs.smartrecruiters.com/{slug}/{item.get('id', '')}",
                    external_id=str(item.get("id", "")),
                    location=where,
                    remote=bool(loc.get("remote")),
                    department=(item.get("department") or {}).get("label", ""),
                    employment_type=(item.get("typeOfEmployment") or {}).get("label", ""),
                    posted_at=parse_date(item.get("releasedDate")),
                    raw=item,
                ))
            if len(data["content"]) < 100:
                reached_end = True
                break
            offset += 100
        _record_completeness(self.name, slug, reached_end)
        return jobs


class WorkableSource(BaseATS):
    name = "workable"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        data = SESSION.get_json(url)
        if not data or not data.get("jobs"):
            return []
        jobs = []
        for item in data["jobs"]:
            desc = strip_html(item.get("description", ""))
            lo, hi, cur = _salary_from_text(desc)
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or data.get("name", slug),
                title=item.get("title", ""),
                url=item.get("url", "") or item.get("application_url", ""),
                external_id=str(item.get("shortcode") or item.get("id", "")),
                location=", ".join(x for x in [item.get("city"), item.get("country")] if x),
                remote=str(item.get("telecommuting", "")).lower() in ("true", "1"),
                department=item.get("department", ""),
                employment_type=item.get("employment_type", ""),
                description=desc,
                posted_at=parse_date(item.get("published_on") or item.get("created_at")),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=item,
            ))
        return jobs


class RecruiteeSource(BaseATS):
    name = "recruitee"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        data = SESSION.get_json(f"https://{slug}.recruitee.com/api/offers/")
        if not data or "offers" not in data:
            return []
        jobs = []
        for item in data["offers"]:
            desc = strip_html(item.get("description", ""))
            lo, hi, cur = _salary_from_text(desc)
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or slug.replace("-", " ").title(),
                title=item.get("title", ""),
                url=item.get("careers_url", "") or item.get("careers_apply_url", ""),
                external_id=str(item.get("id", "")),
                location=", ".join(x for x in [item.get("city"), item.get("country")] if x),
                remote=bool(item.get("remote")),
                department=item.get("department", ""),
                employment_type=item.get("employment_type_code", ""),
                description=desc,
                posted_at=parse_date(item.get("published_at")),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=item,
            ))
        return jobs


class PersonioSource(BaseATS):
    name = "personio"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        from xml.etree import ElementTree

        text = SESSION.get_text(f"https://{slug}.jobs.personio.de/xml")
        if not text:
            return []
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return []
        jobs = []
        for pos in root.iter("position"):
            def field(tag: str) -> str:
                node = pos.find(tag)
                return (node.text or "").strip() if node is not None and node.text else ""

            desc = strip_html(" ".join(
                (n.text or "") for n in pos.iter() if n.tag in ("value", "jobDescription")))
            jobs.append(Job(
                source=self.name, source_kind=self.kind,
                company=company or slug.title(),
                title=field("name"),
                url=f"https://{slug}.jobs.personio.de/job/{field('id')}",
                external_id=field("id"),
                location=field("office"),
                department=field("department"),
                employment_type=field("employmentType"),
                description=desc,
                posted_at=parse_date(field("createdAt")),
                raw={"schedule": field("schedule"), "seniority": field("seniority")},
            ))
        return jobs


class WorkdaySource(BaseATS):
    """Workday - the default for large UK employers (aerospace, pharma, banks)."""

    name = "workday"

    def fetch(self, slug: str, company: str = "") -> list[Job]:
        # slug format: "tenant:wd3:site", e.g. "rollsroyce:wd3:RRCareers"
        parts = slug.split(":")
        if len(parts) != 3:
            return []
        tenant, wd, site = parts
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        jobs, offset = [], 0
        reached_end = False
        while offset < MAX_PER_COMPANY:
            data = SESSION.post_json(
                endpoint,
                {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                headers={"Content-Type": "application/json"},
            )
            if data is None:
                break                          # a failed page - the end was NOT reached
            if not data.get("jobPostings"):
                reached_end = True
                break
            for item in data["jobPostings"]:
                path = item.get("externalPath", "")
                bullets = item.get("bulletFields") or [""]
                jobs.append(Job(
                    source=self.name, source_kind=self.kind,
                    company=company or tenant.replace("-", " ").title(),
                    title=item.get("title", ""),
                    url=f"{base}/{site}{path}",
                    external_id=str(bullets[0] or path),
                    location=item.get("locationsText", ""),
                    description=strip_html(item.get("jobDescription", "")),
                    posted_at=parse_date(item.get("startDate")),
                    raw=item,
                ))
            if len(data["jobPostings"]) < 20:
                reached_end = True
                break
            offset += 20
        _record_completeness(self.name, slug, reached_end)
        return jobs


ATS_ADAPTERS = {
    a.name: a() for a in [
        GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource,
        WorkableSource, RecruiteeSource, PersonioSource, WorkdaySource,
    ]
}

# Probe order for auto-discovery: cheapest and most common first.
_DISCOVERY_ORDER = ["greenhouse", "lever", "ashby", "workable", "recruitee",
                    "smartrecruiters", "personio"]


def discover_ats(slug: str, company: str = "") -> tuple[str | None, list[Job]]:
    """Try each ATS in turn for a company slug; return the first that answers."""
    for name in _DISCOVERY_ORDER:
        try:
            jobs = ATS_ADAPTERS[name].fetch(slug, company)
        except Exception as exc:  # one bad adapter must not stop discovery
            log.debug("%s failed for %s: %s", name, slug, exc)
            continue
        if jobs:
            return name, jobs
    return None, []
