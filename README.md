# Job Scout

**A job finder for university students** - summer internships, year-in-industry
placements and graduate schemes - that pulls tens of thousands of postings from
employers' own career pages, student job boards, community-run GitHub lists and job
APIs, ranks every one against *your* degree, skills and criteria, and runs entirely on
your own computer.

## Download and run it

You need **Python 3.10 or newer** ([python.org/downloads](https://www.python.org/downloads/) -
on Windows, tick *"Add python.exe to PATH"* when installing).

1. **Download:** [**job-scout.zip**](https://github.com/HX7247/job-scout/releases/latest/download/job-scout.zip)
   (or on this page: green **Code** button → **Download ZIP**), and unzip it anywhere.
2. **Run:** double-click **`start.bat`** (Windows). On macOS/Linux, open a terminal in the
   folder and run `./start.sh`.
3. Your browser opens at <http://127.0.0.1:5000>. Press **Run scrape** to fetch jobs.

The first launch sets up a private environment and installs what it needs (a minute or
two); later launches start in seconds. Keep the black window open while you use the
app - closing it stops it. Prefer git? `git clone https://github.com/HX7247/job-scout.git`.

Everything stays on your machine: your job list, criteria, notes and CV never leave it.

## What it does

**Finding jobs**
- Scrapes ~450 employers' own career pages directly (big graduate recruiters plus ~200
  hiring startups), and 20+ boards, APIs and community lists - see the table below.
- Ranks every posting 0-100 against your criteria, CV or LinkedIn export, and says why.
  Placements, internships and graduate schemes are ranked up for students.
- Works for 40+ countries (UK, US, Australia, Canada, Ireland, Europe, Asia...) with the
  right currency, location matching and country-specific boards.
- **Every scan refreshes everything**: all stored postings are rescored, and ones the
  employer has taken down or whose closing date has passed are hidden automatically.

**Filtering and reviewing**
- Filters for job type (placement / internship / graduate), field of work, remote /
  hybrid, visa-sponsor employers (UK and US registers), salary disclosed, startups
  (include / hide / only), postings you've already opened, plus your own custom filters.
- Your filters are saved between visits; opened postings are marked "Opened today".
- A swipe-style **Review** deck to triage quickly.
- Flags for ghost jobs (open 45+ days) and "remote" jobs that actually want office days.

**Per job**
- **Company stability** rating (Established / Stable / Early-stage / Check before
  applying) with every fact and its source - company register, Wikidata, sponsor licence,
  startup stage, hiring activity. Not employee reviews: Glassdoor has no public API.
- Interview prep built from the advert and the employer's own values and practice tests.
- CV and cover-letter review against that specific job.
- Who to contact, with outreach drafts.

**Tracking**
- Export to Excel, or append to your own tracker workbook; status, notes and stars.
- Nudges for applications that have gone quiet for 21+ days.
- LinkedIn profile review, an assistant that takes plain-English requests, and optional
  local sign-in so several people can share one computer with separate criteria.

## Where the jobs come from

| Source | What it is | Region | Needs |
|---|---|---|---|
| **Company career pages** | Employers' own boards on Greenhouse, Lever, Ashby, Workable, Recruitee, Personio, Workday and Oracle HCM - ~450 employers incl. graduate recruiters and YC startups, plus boards found automatically behind TARGETjobs' apply links (see below) | All | - |
| **TARGETjobs** | ~650 placements and ~600 internships, each with a closing date and most with a start date | UK | - |
| **Gradcracker** | STEM placements, internships, graduate jobs | UK | - |
| **RateMyPlacement** (higherin.com) | Year-in-industry placements and internships | UK | - |
| **GradConnection** | Graduate jobs and internships | AU, NZ, SG | - |
| **GitHub job lists** | Community trackers: SimplifyJobs (internships + new grad), vanshb03, zapplyjobs, jobright-ai, plus European, Canadian and Singapore internship lists - ~5,000 listings | Mostly US, some EU/CA/SG | - |
| **Hacker News "Who is hiring"** | The monthly hiring thread | Global | - |
| **Arbeitnow, Remotive, RemoteOK, Jobicy, Himalayas, The Muse** | Open job-board APIs | Global / remote | - |
| **Google Jobs** (via SerpApi) | Pools listings from many boards, incl. LinkedIn and Indeed | All | free key |
| **Adzuna** | Large job aggregator | UK, US, AU + more | free key |
| **Reed** | UK job board | UK | free key |
| **Careerjet** | Job search engine | UK, US, AU + more | free key |
| **Reddit** | r/internships, r/csMajors, r/cscareerquestions (AI filters real postings from chat) | Global | free key |
| **USAJobs** | US federal government jobs and internships | US | free key |
| **Your own feeds** | Any public RSS feed or listing page you add, e.g. your university's | Any | - |

Sources needing a key are skipped (with a note) until you set it. All keys are free;
set them once in a terminal, then restart the app:

```bash
setx SERPAPI_KEY          your_key   # serpapi.com - Google Jobs (250 searches/month free)
setx ADZUNA_APP_ID        your_id    # developer.adzuna.com
setx ADZUNA_APP_KEY       your_key
setx REED_API_KEY         your_key   # reed.co.uk/developers
setx CAREERJET_API_KEY    your_key   # careerjet.com/partners/api
setx REDDIT_CLIENT_ID     your_id    # reddit.com/prefs/apps - "script" app
setx REDDIT_CLIENT_SECRET your_secret
setx USAJOBS_API_KEY      your_key   # developer.usajobs.gov
setx USAJOBS_USER_AGENT   you@example.com
setx COMPANIES_HOUSE_KEY  your_key   # UK company checks in the stability rating
setx ANTHROPIC_API_KEY    your_key   # optional: AI-written reviews and Reddit filtering
```
(macOS/Linux: `export NAME=value` in your shell profile instead of `setx`.)

**Deliberately not used:** Indeed, LinkedIn, Glassdoor, Handshake and ZipRecruiter forbid
scraping in their terms and actively block it (Google Jobs is the legitimate route to
much of their listings). Milkround, Graduate Recruitment Bureau, Bright Network and
Jooble block automated access outright; Prospects and TargetJobs load listings in the
browser only. Details for each are recorded in the source files.

---

The rest of this README is the full documentation.

---

## Your profile, without typing anything

Open **Profile** and point it at your LinkedIn data export. It reads your degree, your
field of study, your institution, your skills, your languages, your job titles and how
long you have worked, then suggests search criteria built from them. Nothing is applied
until you press the button, and the panel lists every inference it made.

### Why an export and not your profile URL

Pasting a profile URL cannot work, and it is worth saying why rather than quietly
failing:

- **Scraping is out.** LinkedIn auth-walls profile pages and their User Agreement
  forbids automated access. The *hiQ* litigation ended in 2022 with LinkedIn winning on
  breach of contract, so "it is public data" is not a defence.
- **Their API is worse than useless here.** Sign In with LinkedIn (OpenID Connect)
  returns your name, email address and photo — more personal data than this app wants,
  and none of the experience, skills or education it needs. Full profile access is a
  partner programme individuals cannot join.

So the app uses the one route that is both permitted and complete: the copy of your own
data LinkedIn hands you on request.

| Route | Where | Precision |
|---|---|---|
| Data export `.zip` | Settings → Data privacy → Get a copy of your data | Best — dates, degree and skills are separate fields |
| Profile `.pdf` | Your profile → More → Save to PDF | One click, but no dates means no experience total |
| Pasted text | Select all on your own profile | Last resort |

### What is kept, and what is not

The export contains your name, email addresses, phone numbers, postal address, date of
birth, your whole connection list and your messages. `jobscout/privacy.py` holds an
**allowlist** — not a blocklist — of what may be written to disk, and every profile
passes through it twice, once at parse time and once at save time.

| Kept | Never kept |
|---|---|
| education level, degree, field of study, institution | name, maiden name, email, phone |
| skills, languages, industries | postal address, postcode, date of birth |
| coarse region (`London, England`) | photo, member id, connections, messages |
| years of experience, recent job titles | the text of your CV, and the path to it |

A field added to the profile does not become storable until its name appears on that
allowlist, which is the point. The audit asserts it, using a fixture export stuffed with
personal data, and fails if any of it survives.

Everything the app holds is listed in plain language at the bottom of the **Profile**
tab, with a delete button per store.

### Signing in

Optional, and it is a lock on *your* data rather than on the job list — the postings are
employers' public adverts and hold nothing about you. An account is a username you
invent, a scrypt hash of your password, and a creation date. There is no email field,
and therefore no password reset by email: that is the honest trade for not holding a way
to contact you.

---

## How it finds jobs

Two tiers, because they fail differently.

**Company boards, at source.** Most career pages are a thin shell over a public ATS
JSON feed. Job Scout calls those feeds directly, so postings arrive before aggregators
index them, with full descriptions and no scraping of rendered HTML.

| Adapter | Endpoint shape |
|---|---|
| `greenhouse` | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| `lever` | `api.lever.co/v0/postings/{slug}?mode=json` |
| `ashby` | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| `smartrecruiters` | `api.smartrecruiters.com/v1/companies/{slug}/postings` |
| `workable` | `apply.workable.com/api/v1/widget/accounts/{slug}` |
| `recruitee` | `{slug}.recruitee.com/api/offers/` |
| `personio` | `{slug}.jobs.personio.de/xml` |
| `workday` | `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/...` (slug form `tenant:wd5:Site`), or `wd{N}.myworkdaysite.com/wday/cxs/...` (slug form `tenant:wd3:Site:myworkdaysite`) |
| `oracle` | `{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions` (slug form `host/siteNumber`) |

SmartRecruiters' API is in the code but returns nothing: `api.smartrecruiters.com/robots.txt`
disallows every crawler except LinkedIn's, and the app obeys robots.txt.

**Harvested boards.** After each scan, apply links from the UK boards (TARGETjobs above all)
are mapped to the ATS board behind them - a Lloyds placement linking to
`lbg.wd3.myworkdayjobs.com` means every Lloyds posting is one request away. New boards go to
`data/companies_harvested.yaml` (git-ignored, safe to delete) and are read from the next
scan; one that comes back empty three scans running is dropped. Turn it off with
`sources.harvest_boards: false`.

**When a source breaks.** Each host is throttled on its own (one slow site no longer holds up
the rest), a 429's `Retry-After` is honoured, and a host that fails four times running is
left alone for ten minutes rather than retried on every board. The scan then compares itself
with the previous one: a source or board that went from postings to none - almost always a
changed page or API, not an empty market - is listed under *Source health* in the sidebar,
with any host still failing.

**Third-party boards.** `arbeitnow`, `remotive`, `themuse`, `jobicy`, `himalayas`,
`remoteok` and `hn_hiring` need no key. `adzuna` and `reed` do, and they are the two
with real UK depth — see *Adding coverage* below.

### The company registry

`data/companies_verified.yaml` maps employers to the ATS that actually answers for them.
It is generated, not hand-written:

```bash
python tools/discover_slugs.py
```

That probes every candidate name against every simple-GET ATS and records what responds.
Add names to `CANDIDATES` in that file and re-run to widen coverage. Watch for false
positives: a hit with only 1–2 jobs is often a squatted slug rather than the real
employer.

Workday needs its own prober, because a Workday board is addressed by a tenant, a data
centre number and a site name, none of which follow from the company name:

```bash
python tools/discover_workday.py
```

The registry currently holds **86 employers**: 41 Greenhouse, 25 Ashby, 9 Workday,
4 SmartRecruiters, 3 Recruitee, 2 Lever, 2 Workable.

The Workday run found 9 of 54 candidates — a deliberately low hit rate, because the
tenant is genuinely unguessable (`astrazeneca:wd3:Careers` and `gsk:wd5:GskCareers`
follow no shared pattern). Those 9 add roughly 8,000 postings from employers the other
adapters cannot reach at all, Dyson and Thales among them. To add one by hand, open the
employer's careers page and read the tenant, `wdN` and site straight out of the URL.

---

## Every kind of job

Job Scout does not assume you want a career-track role. A Saturday job at a coffee
shop, a warehouse shift, a 12-month placement and a permanent engineering post are all
first-class results, and you pick which you want to see.

Two independent labels are worked out for every posting, because they genuinely are
independent - a barista job can be part-time or full-time, and a software job can be
an internship or permanent:

**Job type** &mdash; part-time, full-time, internship, placement / year in industry,
apprenticeship, graduate scheme, weekend, zero-hours, seasonal / temporary, contract /
freelance, voluntary.

**Field of work** &mdash; retail, hospitality & food, care, healthcare, education,
driving & delivery, warehouse & logistics, security, cleaning & facilities, customer
service, software & IT, data, engineering, science, construction & trades, finance,
legal, marketing, sales, HR, admin, creative, operations.

Both are **pure filters**: leave them empty and everything shows. Both are also sort
options, so you can group the list by type or field and browse.

Sources cannot be trusted on this. One scrape produced `FullTime`, `Full-Time`,
`Full Time`, `full_time`, `Mid, fulltime permanent` and `Employee - Permanent` for the
same thing, and two thirds of postings left the field blank entirely. So the raw field
is only a hint and the title decides, falling back to the description.

Where a posting simply never says, the label is left empty and shown as **not stated**
rather than guessed. Two thirds of postings are in that position. Picking a job type
therefore narrows to postings that actually declare it, and *Also show unstated* widens
the result back out; the tickbox does nothing until you pick a type.

Classification is deliberately conservative, because each shortcut produced a
confidently wrong answer on real data:

- **Word boundaries, not substrings.** "pub" matched inside "Global **Pub**lic Sector"
  and filed a machine-learning role under hospitality.
- **Titles, not prose, for the field of work.** A payments company writes "retail", a
  software team writes "delivery", a data role writes "cleaning". Only the title and
  the department *category* are consulted.
- **An allowlist for prose about hours.** A description mentioning an on-call
  "weekend" rota or the company's "fixed term" contracts is not describing this job.
  Only unambiguous phrases like "part-time" are trusted outside the title.
- **Ambiguous words need a noun.** "Delivery" alone is software delivery as often as
  parcels, so driving matches "delivery driver", "courier" or "HGV" instead.

### Consumer employers

`tools/discover_consumer.py` probes retail, hospitality, care and logistics chains
across every ATS *and* Workday in one pass:

```bash
python tools/discover_consumer.py
```

The registry was originally seeded from tech, finance and engineering names only, so
the app could not see a Costa or a Greggs job at all. This run adds the employers that
hire constantly, in volume and part-time &mdash; Asda, JD Sports, Pret A Manger,
Primark, Domino's and Greene King among them.

## Working in any country

`config.yaml` takes a `country`, or infers one from your first location:

```yaml
search:
  country: "US"          # blank = inferred from locations
  locations: ["New York", "United States"]
```

42 markets are registered. The country drives four things that genuinely differ:

- **Remote eligibility.** "Remote - United States" is not a London job, and "Remote -
  Europe" is not a Sydney job. The rule is symmetric: a posting passes if it names your
  country, says worldwide/anywhere, or names your region (EMEA, APAC, LATAM...); it fails
  if it names only other countries. Naming nowhere passes, and the description decides.
- **Salary parsing.** Reads whatever currency the posting quotes - `£32,000`, `$150k`,
  `45.000 EUR`, `₹12,00,000`, `¥6,000,000` - handling comma, dot and Indian digit
  grouping, with per-currency plausibility bounds so a phone number is never read as pay.
  A number with no currency near it is ignored, so "50,000 employees" is not a salary.
- **Which boards are queried.** Adzuna's country code is set from here (19 of the 42
  markets are covered); Reed is skipped outside the UK rather than returning nothing.
- **Tracker output.** Date format (`mm/dd/yyyy` in the US, `dd.mm.yyyy` in Germany,
  `yyyy-mm-dd` in Sweden) and a market-appropriate Source dropdown - Gradcracker and
  RateMyPlacement in the UK, Handshake and ZipRecruiter in the US, StepStone and Xing
  in Germany.

Early-career vocabulary is matched in several languages, so `Praktikum`, `alternance`,
`estágio` and `becario` register as internships alongside `placement` and `co-op`.

**Known gap:** the company registry in `data/companies_verified.yaml` was seeded from a
UK/US list. For another market, add local employers to `tools/discover_slugs.py` and
re-run it - the ATS adapters themselves are country-neutral.

## Visa sponsorship — the international-student filter

`search.visa_sponsorship` is `any`, `prefer` or `require`. This is checked against the
official government register, not inferred from job text.

**United Kingdom.** The Home Office publishes the *Register of licensed sponsors:
workers* as a CSV on GOV.UK, refreshed roughly monthly. Job Scout finds the current
file (the filename carries its date), downloads it once, and matches employers against
its ~125,000 organisations, recording the licence rating (A, B or provisional) and the
routes they may sponsor.

**United States.** The Department of Labor's OFLC LCA disclosure files list every H-1B
filing, so an employer's filing count measures how often they actually hire
international staff. Those files are large and not served at a stable URL, so drop one
at `data/sponsors_US.csv` with an `Employer` column and it will be read the same way.

Matching a trading name to a registered legal name is the hard part, and the UI is
honest about it. Every match shows the registered name it hit and a confidence:

| Match | Meaning |
|---|---|
| `exact` | normalised names are identical |
| `partial` | your company's words are all present in a longer registered name (`Monzo` → `Monzo Bank Ltd`) |
| `fuzzy` | tolerant of word order and spelling (`Arup` → `Ove Arup and Partners International Ltd`) |
| `registered name` | matched on the employer's official registered name, not its trading name |

Trading names are ambiguous, so the matcher escalates. When a plain name match is weak
or ambiguous it looks up the employer's **registered name and company number** on
Wikidata and retries with those &mdash; `Monzo` becomes `Monzo Bank Limited`,
`Rolls-Royce` becomes `Rolls-Royce Holdings plc` (company number `07524813`).

For certainty rather than inference, set a free
[Companies House](https://developer.company-information.service.gov.uk/) key:

```bash
setx COMPANIES_HOUSE_KEY your_key
```

With it, the company number is resolved to the authoritative registered name before
matching. Names collide; numbers do not. Without it the app still works and simply
labels uncertain matches as uncertain.

A subset match is inference, not identification, so its confidence is capped at 0.85,
and at 0.60 whenever another registered name fits equally well. `Stripe` matches both
*Stripe Partners* and *Stripe Consulting Limited* &mdash; neither is Stripe Inc, and the
UI says so rather than picking one silently.

Where the job names a town, it is used to disambiguate, because the register records
each sponsor's town. Ambiguous hits are flagged rather than silently resolved — company
names collide, and `Atkins` matches both the engineering firm and unrelated small
businesses. **Always check the registered name shown before relying on it.**

Absence from the register is reported as "no licence found", never "will not sponsor" —
smaller employers apply for a licence when they find the right person.

## Review deck &mdash; swipe through the queue

The **Review** tab deals cards one at a time, best match first. Drag them, or use the
arrow keys:

| Gesture | Key | What it does |
|---|---|---|
| Swipe right | `→` | Save it — onto your tracker, with the apply link |
| Swipe left | `←` | Pass (status becomes dismissed) |
| Swipe up | `↑` | Star and save |
| &mdash; | `U` | Undo the last decision |

Each card carries a **reason**: what in the advert lines up with what you have actually
done, what they also want that you have not shown, and how much to trust that. Written
from the match reasons by default, or by Claude when `ANTHROPIC_API_KEY` is set — and in
that case it is also asked for the strongest reason to swipe *left*, because a card that
only flatters you is a card you cannot use.

Swiping right **saves**. The posting goes onto your tracker with the company, role,
deadline, location, salary and apply link filled in, and the panel beside the deck offers
the interview brief, a cover-letter draft, and the employer's own form.

**Nothing is ever submitted for you, and there is no setting that changes that.** An
application cannot be recalled, and a half-tailored one burns you with that employer for
the season. Published reviews of tools that do auto-submit report exactly that: forms
fired at roles outside the user's own filters, with generated documents that still needed
editing. So the app does every part except the click.

The cover letter is drafted by Claude when a key is set and from a template otherwise.
Either way it is a draft with bracketed gaps only you can fill, it signs off with
`[Your name]` because the app does not know your name, and anything in it that reads as
machine-written is flagged with what to put instead.

---

## Interview and assessment prep

Open a saved job and press **Prepare for the interview**, or use the tab in the job
drawer. The brief is assembled from four sources, in descending order of reliability:

1. **The advert itself.** Employers say more than candidates notice — "online
   assessment", "take-home", "situational judgement", sometimes the platform by name.
   Thirteen stage types are detected, each quoting the words that triggered it.
2. **Which tracking system the posting came from**, which implies a process shape.
3. **A researched map of who uses which test provider.** UK graduate hiring is
   concentrated: SHL across the banks, Cappfinity across the Big Four and the NHS,
   Watson Glaser across the law firms, HackerRank and CodeSignal across engineering.
   Each pairing carries its reasoning, and an unknown employer is reported as unknown
   rather than guessed at.
4. **The employer's own values page**, fetched politely and with `robots.txt` respected,
   because values-based and situational-judgement questions come from it almost verbatim.

It also links **practice papers published for that exact employer**. AssessmentDay
publishes a pack per employer per test type; `tools/discover_assessments.py` reads their
sitemap and builds a lookup of 96 employers and 182 packs, so asking about Amazon or the
NHS or Rolls-Royce returns practice in the format they actually send. The questions stay
on their site — the app links, it does not copy.

Then: the questions, each with **what a good answer has to contain** rather than the
answer itself; which of *your* projects is the evidence for each skill the posting names;
the gaps they will probe and how to admit them; and where other candidates prepare, free
tiers marked.

### It does not write your answers

Deliberately. Reciting a prepared paragraph is the thing that loses interviews — it is
audible, and it collapses at the first follow-up. So the brief gives you the question,
why it is being asked, what the answer needs, and which of your own work is the material.
You supply the sentences.

`jobscout/humanise.py` backs this up in code rather than in a prompt. It holds forty-odd
phrasings that read as generated — *delve*, *leverage*, *passionate about*, *a testament
to*, "not only X but also Y", three-em-dash paragraphs, five sentences of identical
length — and checks every draft the app produces, reporting what to change. It does not
claim to defeat an AI detector; those are unreliable in both directions and anyone
selling certainty about them is selling something. It removes the phrasings that make a
reader's eyes glaze, which is the part that actually costs you.

## Company background

Open a job and the panel shows what the employer actually is: industry, size band,
founding year, headquarters and website, from **Wikidata** — a free, open, no-key API
licensed CC0 and built for reuse. Results are cached in `data/company_cache.json`.

**Glassdoor is deliberately not used.** Its public API was closed to new developers in
2022 and is now enterprise-partner only, so there is no first-party way to read ratings
or reviews. Scraping the site would breach its terms and run into bot management that
would put your own IP and account at risk. The UI says so rather than pretending the
data is unavailable for technical reasons.

## Career stage

`search.career_stage` is one of `student`, `graduate`, `professional`, `senior`, `any`.
It was added after testing the app as each of those users, which exposed three faults:

| Stage | What changes |
|---|---|
| `student` | Boosts placement/internship wording; drops senior titles; leads outreach with university alumni |
| `graduate` | Same filtering, but messages say "recent graduate" rather than "student" |
| `professional` | **Inverts** the level filter - junior and intern titles are penalised instead; outreach leads with recruiters, not alumni |
| `senior` | As professional, and introduces you as a senior engineer |
| `any` | No level adjustment in either direction |

## Working arrangement

`search.work_modes` accepts any of `remote`, `hybrid`, `onsite`. Job Scout reads the
arrangement out of the title, location and the opening of the description. A posting
that states no arrangement is never dropped on this — it is simply not rewarded, since
silence is not evidence.

Outreach messages are built from your stage, so a professional never sends "I'm a MEng
student", and the skills named are the ones this job actually asked for rather than the
longest strings on your CV.

## How it ranks

`hard_filter` drops a posting outright; everything surviving gets a 0–100 score.

Hard filters: excluded keywords, required keywords, excluded companies, max age,
location eligibility, and senior-level titles when the search is early-career.

Location eligibility is stricter than a substring match. "Remote — United States" is not
a London job, so a remote posting scoped to a country you cannot sit in is rejected
unless it also carries a global marker (`worldwide`, `EMEA`, `Europe`, `UK`).

Score components, weighted from `config.yaml`:

| Component | Default weight | What it measures |
|---|---|---|
| `title` | 30 | fuzzy match against your target titles |
| `cv_skills` | 25 | overlap between the posting's skills and your CV |
| `keywords` | 20 | how many of your "nice to have" terms appear |
| `location` | 12 | preferred locations score higher, in list order |
| `recency` | 8 | freshly posted ranks higher |
| `salary` | 5 | stated salary against your floor |

Plus adjustments: early-career wording in the title `+8`, clearly senior `−18`, and
`+3` for coming straight from a company board rather than an aggregator.

Every row records *why* it scored what it did, and the UI shows that list — along with
which of your CV skills matched and which listed skills you are missing.

---

## Your CV, if you would rather use one

A LinkedIn import is the better input because it is structured. A CV still works as a
fallback: point `config.yaml` at it, and PDF, DOCX and TXT are all read.

```yaml
profile:
  cv_path: "C:/path/to/your-CV.docx"
  school: "Your University"
  school_linkedin_slug: "your-university"
  extra_skills: []
```

Parsing extracts skills against a vocabulary grouped by domain (engineering, data,
cloud, safety/quality, finance, soft), plus the qualification and the subject. The text
itself is **not** kept, and there is no longer any code path that could store the email
address or phone number on it — `cv.py` used to collect both and no longer does.

### Skills and filters the app does not know about

The vocabulary covers about three hundred technical terms and will never cover your
niche instrument, your society or an in-house tool. Add your own on the **Profile** tab
and they score exactly like recognised ones.

The same tab takes filters of your own, for anything the eleven job types and
twenty-three fields of work do not cover:

| Mode | What it does |
|---|---|
| `exclude` | Never show postings mentioning these words |
| `require` | Only show postings mentioning them |
| `boost` | Rank them higher, but still show the rest |

Each can be scoped to the title, company, location or description — "graduate" in a
description is noise, "graduate" in a title is the job — and a trailing `*` matches
loosely (`prosthe*`). Terms match whole words, so `pub` does not match "Global Public
Sector". Tick **show as a checkbox** and the filter joins the sidebar with its own live
count.

---

## Who to talk to

Open a job and switch to **Who to talk to**.

This builds deep links into LinkedIn's *own* search — it does not scrape, fetch, store
or index any profile. You click a link, LinkedIn renders it while you are logged in, and
you see results with your real degree-of-connection. That is both permissible and more
useful than anything a scraper would return.

Links generated, in the order worth trying:

1. **Alumni of your university now at the company** — much the highest reply rate
2. Early careers / graduate recruitment
3. Recruiters
4. People already doing the role
5. Managers and leads
6. The role on LinkedIn Jobs, which often names the poster

Each comes with a drafted connection note (kept under LinkedIn's 300-character limit)
and a longer follow-up message, both filled in from your course, school and the matched
skills for that specific job.

**On "verified" users:** LinkedIn's identity-verification badge is not exposed through
any API and is not a filter you can encode in a search URL — it is only visible on the
profile itself. So Job Scout cannot filter by it, and says so in the UI rather than
implying otherwise. Shared school and confirmed current employer are addressable, and
are stronger outreach signals anyway.

Anything the posting itself publishes — a named recruiter, a contact email in a Hacker
News listing — is surfaced separately under "Named on the posting itself".

---

## The spreadsheet tracker

Built to match a typical student internship-tracker workbook exactly: 21 columns A–U, the same five column
groups, the same dropdown lists, and the same `Days Left` / `Alert` formulas.

The split follows the grouping already in your sheet:

| Auto-filled | You fill in |
|---|---|
| Company, Role Name, Deadline, Application Link, Location, Salary, Duration, Open Date, Source | Start Date, CV, Cover Letter, Applied?, Date Applied, Status, Got as far as, Next Action, Next Action Date, Notes |

`Days Left` and `Alert` stay formulas and are never typed.

**Append to a copy of my workbook** (preferred) — copies your file, appends the current
filtered view to the Tracker sheet, extends the dropdown ranges to cover the new rows,
and leaves `Dashboard`, `Calc` and `Settings` untouched so your Sankey keeps working.
Your original is never modified; a dated copy lands in `out/`.

**Build standalone tracker** — a fresh workbook with the same Tracker layout, its own
Settings sheet, and a dashboard using native Excel charts: funnel by round reached,
outcome split, and deadline KPIs. This is deliberately *not* a copy of your Sankey —
that is a hand-built conditional-formatting grid driven by `Calc`, and the append mode
preserves the real thing rather than reproducing it badly.

Exports follow whatever filters are active, so star a shortlist and export just that.

---

## The assistant

Plain-English edits to the search. Two engines, same vocabulary:

- **Claude Opus 5** when `ANTHROPIC_API_KEY` is set — handles free-form phrasing.
- **A rule-based parser** otherwise, covering the common commands.

The UI labels which one answered. The model never touches the database directly: it can
only propose actions from a fixed list, which are validated and executed by
`apply_actions`. Anything that deletes rows or hits the network is gated behind an
explicit confirm.

Things it understands either way:

```
only London and hybrid roles
raise the score floor to 50 and sort by date
I do not want anything unpaid or commission only
exclude company Deloitte
show me solidworks roles
posted in the last 14 days
which sources are working?
how many jobs are stored?
export the tracker
```

Criteria changes trigger an automatic re-score of everything stored. Rows that no longer
qualify are pruned — unless you have starred them or moved them off "new", which
protects anything you have engaged with.

---

## Adding coverage

The single biggest win available is two free API keys:

```bash
setx ADZUNA_APP_ID   your_id      # developer.adzuna.com
setx ADZUNA_APP_KEY  your_key
setx REED_API_KEY    your_key     # reed.co.uk/developers
```

Both are configured in `config.yaml` already and skipped with a "no API key" note until
the variables exist. Adzuna in particular carries the UK volume that the free keyless
boards do not.

Beyond that: add employers to `tools/discover_slugs.py` and re-run it; add Workday
tenants by hand.

### The community/forum/student-board tier

Five more sources, in `jobscout/sources/student_boards.py`, `reddit.py`,
`github_boards.py`, `usajobs.py` and `customfeeds.py` - covering exactly the places
people actually go looking for internships beyond company boards and aggregators:

- **Gradcracker** and **RateMyPlacement** (now trading as **higherin.com**) - UK
  student-specific boards, scraped directly (HTML, not JSON), on by default, no key
  needed. **Bright Network** was evaluated too but sits behind a Cloudflare JS
  challenge that blocks every plain HTTP request including `/robots.txt` itself, so
  its adapter is real but permanently returns nothing - it is not enabled by default.
- **GitHub internship trackers** (`SimplifyJobs/Summer2026-Internships` and
  `vanshb03/Summer2026-Internships` - the two still-maintained ones of the three
  checked) - the community-run READMEs students actually watch. Keyless, on by default.
- **Reddit** (r/internships, r/csMajors, r/cscareerquestions by default, see
  `sources.reddit_subreddits`) - reddit.com closed off unauthenticated JSON access in
  2023, so this goes through a real registered app instead:
  ```bash
  setx REDDIT_CLIENT_ID     your_id       # reddit.com/prefs/apps, "script" type
  setx REDDIT_CLIENT_SECRET your_secret
  ```
  Skipped with a "no API key" note until both exist, same as Adzuna/Reed. Because most
  posts in these subreddits are questions or discussion rather than real openings, an
  LLM agent (gated behind `ANTHROPIC_API_KEY`, same as the CV review and LinkedIn
  review features) decides which posts are genuine postings and extracts
  company/role/location - never inventing one, never trusting a value it can't find
  verbatim back in the post. Without a key, a tight rule-based fallback (bracket tags
  like `[Hiring]`, or matching post flair) keeps the feature usable, just more
  conservative.
- **USAJobs** - the official US federal jobs API, US-only, free key:
  ```bash
  setx USAJOBS_API_KEY     your_key        # developer.usajobs.gov
  setx USAJOBS_USER_AGENT  you@example.com  # the email you registered the key with
  ```
- **Custom feeds** - the honest answer to "what about my university's career site?".
  Handshake, which most UK/US universities actually run their careers service on,
  requires a student login and its ToS prohibits scraping, so nothing here touches it
  or any other login-walled portal. Instead, `sources.custom_feed_urls` in
  `config.yaml` takes any public RSS feed or listing page URL you give it - your own
  university's, if it publishes one, or anything else not otherwise covered - and Job
  Scout fetches and classifies it. Empty by default: nothing is fetched until you add
  a URL yourself.

- **GradConnection** (Australia, New Zealand, Singapore) - graduate jobs and
  internships, keyless, on by default; it skips itself for other markets.
- **Careerjet** - a job search engine with a documented API, and the legitimate route
  to the kind of breadth people use Indeed for (Indeed itself, Glassdoor and LinkedIn
  prohibit scraping and are never touched). Free publisher key:
  ```bash
  setx CAREERJET_API_KEY  your_key        # careerjet.com/partners/api
  ```
- **Hacker News "Who is hiring"** - the monthly forum thread, via its public API.
- **Google Jobs**, through SerpApi (Google has no job-seeker API and forbids scraping
  its results; SerpApi is a commercial API that returns them as JSON). Google Jobs pools
  listings from many boards, LinkedIn and Indeed included, so it is the widest single
  source. The free plan is 250 searches a month; the app makes one per search term per
  scan, and a re-scan within 30 minutes is served from cache:
  ```bash
  setx SERPAPI_KEY  your_key              # serpapi.com
  ```
- **More GitHub trackers** - SimplifyJobs (internships and new grad), vanshb03,
  zapplyjobs (incl. ML internships), jobright-ai (software, data, product new grad), and
  European, Canadian and Singapore internship lists: ~5,000 live listings, shared
  fairly between repos. "{year}" repo names roll over by season with no code change.

Checked and **not** feasible (all recorded in `jobscout/sources/grad_boards.py`):
Prospects and TargetJobs render their listings in the browser, so the page holds no
postings; Milkround times out every plain request; Jooble's API sits behind a
Cloudflare browser challenge; Civil Service Jobs needs a session-keyed search;
Graduate Recruitment Bureau answers every request, robots.txt included, with a 403;
ZipRecruiter's robots.txt disallows its search pages, which serve a bot challenge.
Handshake needs a student login and its terms forbid scraping - never attempted.

### What every scan refreshes

Not only the postings it fetched. At the end of each scan: every stored posting is
rescored against today's criteria, profile and scoring rules; postings an employer has
taken down (judged only for company boards read in full - never for a board cut off at
the per-company cap, and never for aggregators, which only return a page of results) or
whose closing date has passed are hidden, unless you starred them or moved them off
"new", in which case they stay with a **No longer listed** badge; startup tags and
sponsor-licence matches are re-applied; and once the extra company list is two weeks
old it is rebuilt (`sources.registry_refresh_days`, 0 to turn off). A posting that comes
back is live again. Company stability ratings in the list expire after seven days.

### Company career pages and startups

`data/companies_extra.yaml` adds 353 employers' own career boards on top of the
hand-verified registry: UK graduate/placement employers (many on Workday) and hiring
startups from the public [yc-oss](https://github.com/yc-oss/api) YC company dataset.
Each was probed and kept only if its board returned real postings and its name matched,
so a startup never inherits someone else's board through a slug collision. Re-run:

```bash
python tools/discover_startups.py
```

Startups are tagged (`startup: true` only when founded within ~12 years AND under ~500
staff are both known), and the Positions sidebar has **Include / Hide / Startups only**.
"Hide" keeps employers the registry knows nothing about.

### Company stability

Every posting's detail panel rates the employer - *Established*, *Stable*,
*Early-stage*, *Check before applying*, or *Not enough data* - and lists each fact
behind it with its source: Companies House status, insolvency history, overdue accounts
and age; Wikidata headcount and founding year (only when the match is confirmed, never a
namesake's); the Home Office sponsor-licence rating; startup tags; and how many roles
the employer has open and how long they have lingered. It is **not** employee reviews:
Glassdoor has had no public API since 2022. The UK register checks need a free key:

```bash
setx COMPANIES_HOUSE_KEY  your_key      # developer.company-information.service.gov.uk
```

### Saved filters and "viewed"

The Positions sidebar - search box, sliders, checkboxes, sort, startup filter - is
remembered between visits, per account when signed in. Opening a posting marks it as
viewed: the row drops to regular weight and says "Opened today", and **Hide ones I've
opened** filters them out.

All of these register through the same `BaseAggregator`
contract as the rest of `sources.aggregators` in `config.yaml`, degrade to an empty
list on any failure, and route every request through the same polite, robots.txt-aware
`SESSION`.

---

## Layout

```
job-scout/
  app.py                  Flask server and JSON API
  config.yaml             all search criteria
  jobscout/
    models.py             the canonical Job record, null-normalised
    http.py               polite HTTP: robots.txt, per-host rate limit, Retry-After,
                          per-host circuit breaker, disk cache
    harvest.py            grows the company list from job boards' apply links
    sources/ats.py        nine company-board adapters
    sources/aggregators.py nine third-party board adapters
    sources/student_boards.py  Gradcracker, RateMyPlacement/higherin.com, Bright Network
    sources/targetjobs.py TARGETjobs placements and internships (its search JSON)
    sources/reddit.py     r/internships et al, via Reddit's OAuth API + an LLM filter
    sources/github_boards.py   the community-run internship-tracker READMEs
    sources/usajobs.py    the official US federal jobs API
    sources/customfeeds.py     your own RSS feed or public listing page, opt-in
    sources/grad_boards.py     GradConnection (AU/NZ/SG) and the Careerjet API
    stability.py          company stability rating from public records, with evidence
    cv.py                 local document parsing and skill extraction
    linkedin_import.py    reads your LinkedIn export - no scraping, see the docstring
    privacy.py            the allowlist that decides what may be stored, plus scrubbing
    accounts.py           local sign-in: a username and a scrypt hash, nothing else
    rules.py              filters you write yourself
    interview.py          interview and assessment prep for one posting
    humanise.py           catches writing that reads as generated
    why.py                why a posting suits you, for the swipe card
    scoring.py            hard filters and the 0-100 score
    store.py              SQLite: dedupe, status, notes, run history
    pipeline.py           run() for a full scrape, rescore() for a network-free re-rank
    linkedin.py           deep links and outreach drafts (no scraping)
    tracker.py            Excel export, both modes
    assistant.py          natural language to validated actions
  tools/discover_slugs.py       company registry generator
  tools/discover_startups.py    graduate employers + hiring startups -> companies_extra.yaml
  tools/discover_assessments.py employer practice-pack index
  data/                   SQLite db, company registry, HTTP cache
  out/                    generated workbooks
```

## Auditing

```bash
python tools/audit.py
```

98 checks across every module: null and empty inputs, unknown countries, garbage dates,
double-escaped HTML, missing rows, bad statuses, empty exports, config-free scoring, and
a regression test for each bug found so far. It reports findings rather than raising, so
one failure does not hide the rest.

Twenty-five of them are privacy assertions, run against a fixture LinkedIn export
deliberately stuffed with a name, an email address, a phone number, a postal address, a
date of birth and a connection list. They fail if any of it reaches the derived profile,
if the profile object grows a field for it, if a password appears in the accounts
database, or if an apply URL carries an email address into the tracker.

## Housekeeping

- `python run_scrape.py` runs a scrape headlessly.
- HTTP responses are cached for 30 minutes (`data/cache/`), so re-runs are cheap; delete
  that directory to force fresh fetches. Responses over 2MB are not cached, and the
  cache is pruned after every scrape.
- `sources.max_per_company` (default 400) caps how many postings are taken from any one
  employer. Workday's page size is fixed at 20 by the server, so 400 is 20 requests.
- `PoliteSession` reads `robots.txt`, rate-limits per host, and identifies itself. The
  keyed and JSON API endpoints skip the robots check because they are documented APIs,
  not crawlable pages.
