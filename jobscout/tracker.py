"""Excel tracker export, built to match a student's existing internship-tracker workbook.

Column contract (21 columns, A-U) - grouped by *when you fill them in*:

  auto-filled by Job Scout        you fill in
  ------------------------        -----------
  A  Company                      K  CV
  B  Role Name                    L  Cover Letter
  C  Deadline                     M  Applied?
  D  Application Link             N  Date Applied
  E  Location                     O  Status
  F  Salary                       P  Got as far as
  G  Duration   (default)         Q  Next Action
  H  Start Date                   R  Next Action Date
  I  Open Date                    S  Notes
  J  Source

  T  Days Left  } formulas, never typed
  U  Alert      }

NO PERSONAL DATA IS WRITTEN
---------------------------
Every auto-filled column comes from the employer's advert - company, role, deadline, link,
location, salary, source. None of them come from the user's profile, and there is no
column for a name, an email or a phone number. The one place a leak could get in is the
application link, because some aggregators append the recipient's address to the URL as a
tracking parameter, so links are scrubbed before they are written. The Notes column is
yours and is never touched.

Two modes:
  append_into()  - copy the user's real workbook and add rows, leaving their
                   Dashboard (a hand-built Sankey on a conditional-format grid)
                   completely untouched. This is the preferred path.
  export_new()   - build a standalone workbook from scratch, with its own
                   Settings lists, validations and a native-chart dashboard.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

import openpyxl

from . import geo
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS = ["Company", "Role Name", "Deadline", "Application Link", "Location", "Salary",
           "Duration", "Start Date", "Open Date", "Source", "CV", "Cover Letter",
           "Applied?", "Date Applied", "Status", "Got as far as", "Next Action",
           "Next Action Date", "Notes", "Days Left", "Alert"]

GROUPS = [(1, 4, "1.  WHEN YOU FIND A ROLE"), (5, 10, "2.  DETAIL  (all optional)"),
          (11, 15, "3.  WHEN YOU APPLY"), (16, 19, "4.  AS IT MOVES"),
          (20, 21, "AUTO - DON'T TYPE")]

WIDTHS = {"A": 22, "B": 32, "C": 11, "D": 22, "E": 18, "F": 11, "G": 10, "H": 11,
          "I": 11, "J": 19, "K": 12, "L": 12, "M": 9, "N": 12, "O": 16, "P": 15,
          "Q": 24, "R": 12, "S": 30, "T": 9, "U": 24}

LISTS = {
    "lst_Status": ["Not applied yet", "Applied", "Online test", "1st interview",
                   "2nd / AC", "Final round", "Offer", "Accepted", "Rejected",
                   "Ghosted", "Withdrawn"],
    "stg_Names": ["Applied", "Online test", "1st interview", "2nd / AC", "Final round"],
    "out_Names": ["Rejected", "Ghosted", "Withdrawn", "Still live", "Offer"],
    "lst_Doc": ["Not started", "In progress", "Ready", "Sent", "Not needed"],
    "lst_Dur": ["3 months", "6 months", "9 months", "1 Year", "13 months", "Other"],
    "lst_Source": ["Company website", "LinkedIn", "Bright Network", "RateMyPlacement",
                   "Gradcracker", "Indeed", "Milkround", "Uni careers service",
                   "Careers fair", "Referral", "Other"],
    "lst_Closed": ["Rejected", "Ghosted", "Withdrawn", "Accepted"],
}

DAYS_LEFT = '=IF(OR($A{r}="",$C{r}=""),"",$C{r}-TODAY())'
ALERT = (
    '=IF($A{r}="","",IF(AND($M{r}<>TRUE,$C{r}<>"",$C{r}<TODAY()),"Deadline passed",'
    'IF(AND($M{r}<>TRUE,$C{r}<>"",$C{r}-TODAY()<=7),"Closes in "&TEXT($C{r}-TODAY(),"0")&" days",'
    'IF(AND($R{r}<>"",$R{r}<=TODAY(),COUNTIF(lst_Closed,$O{r})=0),"Do: "&$Q{r},'
    'IF(AND($O{r}="Applied",$N{r}<>"",TODAY()-$N{r}>21),"Chase - quiet "&TEXT(TODAY()-$N{r},"0")&" days",'
    'IF(AND(COUNTIF(lst_Closed,$O{r})>0,$O{r}<>"Accepted",$P{r}=""),"Set Got as far as",'
    'IF(AND($M{r}<>TRUE,$C{r}=""),"No deadline yet","")))))))'
)

INK = "1F2937"
HEAD_FILL = PatternFill("solid", fgColor="1E3A5F")
GROUP_FILLS = ["DCE6F1", "EAEFF5", "E3ECE4", "F5EFE3", "EDEDED"]
AUTO_FILL = PatternFill("solid", fgColor="F4F6F8")
THIN = Side(style="thin", color="C8CDD4")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


# --------------------------------------------------------------------- mapping
def _iso_to_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


# Query parameters that carry an identity rather than a destination. Aggregators add
# these, and a tracker full of links with your email in them defeats the point.
_TRACKING_KEYS = re.compile(
    r"^(utm_\w+|email|e|mail|user|uid|u|candidate|cid|applicant|sub_?id|aff|"
    r"referrer_?id|mid|contact|token|session|fbclid|gclid|msclkid)$", re.I)


def safe_url(url: str) -> str:
    """Drop query parameters that identify a person rather than the posting.

    Keeps everything the employer needs to find the req - job IDs, board slugs, gh_jid -
    and removes the rest. When in doubt the parameter stays: a broken link is worse than
    a tracking pixel, and the destructive choice needs a reason.
    """
    if not url or "?" not in url:
        return url or ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_KEYS.match(k) and "@" not in v]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


def job_to_row(job: dict, date_format: str = "dd/mm/yyyy") -> list:
    """Map one stored job to the 19 typed columns (T and U are formulas)."""
    source = ("Company website" if job.get("source_kind") == "ats_direct"
              else {"linkedin": "LinkedIn"}.get(job.get("source", ""), "Other"))
    salary = job.get("salary_display") or ""
    return [
        job.get("company", ""),                    # A Company
        job.get("title", ""),                      # B Role Name
        _iso_to_date(job.get("closes_at")),        # C Deadline
        safe_url(job.get("url", "")),              # D Application Link
        job.get("location", ""),                   # E Location
        salary,                                    # F Salary
        "1 Year",                                  # G Duration (placement default)
        None,                                      # H Start Date - you fill
        _iso_to_date(job.get("posted_at")),        # I Open Date
        source,                                    # J Source
        "Not started",                             # K CV
        False,                                     # L Cover Letter
        False,                                     # M Applied?
        None,                                      # N Date Applied
        "Not applied yet",                         # O Status
        None,                                      # P Got as far as
        None,                                      # Q Next Action
        None,                                      # R Next Action Date
        None,                                      # S Notes
    ]


# ------------------------------------------------------------- append into real
def append_into(jobs: list[dict], source_workbook: str | Path,
                out_path: str | Path, dedupe: bool = True, home=None) -> dict:
    """Copy the user's workbook and append job rows to the Tracker sheet.

    Never writes to the original. Their Dashboard, Calc and Settings are carried
    over untouched, so the Sankey keeps working.
    """
    source_workbook, out_path = Path(source_workbook), Path(out_path)
    if not source_workbook.exists():
        raise FileNotFoundError(source_workbook)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_workbook, out_path)

    date_format = home.date_format if home else "dd/mm/yyyy"
    wb = openpyxl.load_workbook(out_path)
    if "Tracker" not in wb.sheetnames:
        raise ValueError("workbook has no 'Tracker' sheet")
    ws = wb["Tracker"]

    existing = set()
    first_free = 3
    for r in range(3, ws.max_row + 2):
        company = ws.cell(r, 1).value
        if company:
            existing.add((str(company).strip().lower(),
                          str(ws.cell(r, 2).value or "").strip().lower()))
            first_free = r + 1
        elif r >= first_free:
            first_free = r
            break

    added = skipped = 0
    row = first_free
    for job in jobs:
        key = (job.get("company", "").strip().lower(), job.get("title", "").strip().lower())
        if dedupe and key in existing:
            skipped += 1
            continue
        for col, value in enumerate(job_to_row(job), start=1):
            cell = ws.cell(row, col, value)
            if col == 3 or col == 9:                       # date columns
                cell.number_format = date_format
            if col == 4 and value:                          # hyperlink the apply link
                cell.hyperlink = value
                cell.style = "Hyperlink"
        ws.cell(row, 20, DAYS_LEFT.format(r=row))
        ws.cell(row, 21, ALERT.format(r=row))
        existing.add(key)
        added += 1
        row += 1

    last_row = row - 1
    _extend_validations(ws, last_row)
    if ws.auto_filter.ref:
        ws.auto_filter.ref = f"A2:U{last_row}"

    wb.save(out_path)
    return {"added": added, "skipped_duplicates": skipped, "first_row": first_free,
            "last_row": last_row, "path": str(out_path)}


def _extend_validations(ws, last_row: int) -> None:
    """Stretch existing dropdown ranges to cover the rows we just appended.

    The source workbook's validations stop at row 152; without this, appended rows
    below that get no dropdowns and the Status column stops being pickable.
    """
    pattern = re.compile(r"^([A-Z]{1,2})(\d+):([A-Z]{1,2})(\d+)$")
    for dv in ws.data_validations.dataValidation:
        spans = []
        for cell_range in str(dv.sqref).split():
            match = pattern.match(cell_range)
            if match and int(match.group(4)) < last_row:
                spans.append(f"{match.group(1)}{match.group(2)}:{match.group(3)}{last_row}")
            else:
                spans.append(cell_range)
        dv.sqref = MultiCellRange(" ".join(spans))


# ----------------------------------------------------------------- new workbook
def _build_settings(wb, home=None) -> None:
    ws = wb.create_sheet("Settings")
    LISTS["lst_Source"] = geo.tracker_sources(home)
    ws["A1"] = "Settings"
    ws["A1"].font = Font(bold=True, size=14, color=INK)
    ws["A2"] = ("The dropdown lists used by the Tracker. Add your own options to any "
                "list and they appear in the dropdowns straight away.")
    ws["A3"] = "Keep the three grey columns filled in - the Dashboard reads them."

    layout = [("B", "Status", "lst_Status"), ("C", "Counts as an application?", None),
              ("D", "Round (1-5)", None), ("E", "Outcome", None),
              ("G", "Rounds (flow chart)", "stg_Names"), ("I", "Outcomes", "out_Names"),
              ("K", "Document state", "lst_Doc"), ("M", "Duration", "lst_Dur"),
              ("O", "Source", "lst_Source"), ("Q", "Closed statuses", "lst_Closed")]

    # The three derived columns the dashboard needs, aligned to lst_Status order.
    is_app = [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    rounds = [0, 1, 2, 3, 4, 5, 5, 5, 1, 1, 1]
    outcome = ["Applied", "Still live", "Still live", "Still live", "Still live",
               "Still live", "Offer", "Offer", "Rejected", "Ghosted", "Withdrawn"]

    for col, header, list_name in layout:
        ws[f"{col}4"] = header
        ws[f"{col}4"].font = Font(bold=True, color="FFFFFF")
        ws[f"{col}4"].fill = HEAD_FILL
        ws.column_dimensions[col].width = 22
        if list_name:
            for i, value in enumerate(LISTS[list_name]):
                ws[f"{col}{5 + i}"] = value

    for i, value in enumerate(is_app):
        ws[f"C{5 + i}"] = value
    for i, value in enumerate(rounds):
        ws[f"D{5 + i}"] = value
    for i, value in enumerate(outcome):
        ws[f"E{5 + i}"] = value

    spans = {"lst_Status": "B5:B15", "stg_Names": "G5:G9", "out_Names": "I5:I9",
             "lst_Doc": "K5:K9", "lst_Dur": "M5:M10",
             "lst_Source": f"O5:O{4 + len(LISTS['lst_Source'])}",
             "lst_Closed": "Q5:Q8", "st_IsApp": "C5:C15", "st_Stage": "D5:D15",
             "st_Outcome": "E5:E15", "st_Status": "B5:B15"}
    for name, span in spans.items():
        ref = f"Settings!${span[0]}${span[1:].split(':')[0][1:]}:" \
              f"${span.split(':')[1][0]}${span.split(':')[1][1:]}"
        wb.defined_names.add(openpyxl.workbook.defined_name.DefinedName(name, attr_text=ref))


def _build_tracker(wb, jobs: list[dict], rows: int = 300, home=None):
    ws = wb.create_sheet("Tracker", 0)
    date_format = home.date_format if home else "dd/mm/yyyy"

    for (start, end, label), fill in zip(GROUPS, GROUP_FILLS):
        ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=end)
        cell = ws.cell(1, start, label)
        cell.font = Font(bold=True, size=10, color=INK)
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="left", vertical="center")

    for col, header in enumerate(HEADERS, start=1):
        cell = ws.cell(2, col, header)
        cell.font = Font(bold=True, size=10, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 30

    for letter, width in WIDTHS.items():
        ws.column_dimensions[letter].width = width
    ws.column_dimensions.group("E", "J", hidden=False)   # collapsible DETAIL block

    for i, job in enumerate(jobs):
        row = 3 + i
        for col, value in enumerate(job_to_row(job), start=1):
            cell = ws.cell(row, col, value)
            cell.border = BORDER
            if col in (3, 9):
                cell.number_format = date_format
            if col == 4 and value:
                cell.hyperlink = value
                cell.style = "Hyperlink"

    for row in range(3, rows + 3):
        for col in (20, 21):
            cell = ws.cell(row, col,
                           (DAYS_LEFT if col == 20 else ALERT).format(r=row))
            cell.fill = AUTO_FILL
            cell.border = BORDER

    validations = [("O3:O{}", "lst_Status"), ("P3:P{}", "stg_Names"),
                   ("K3:L{}", "lst_Doc"), ("G3:G{}", "lst_Dur"),
                   ("J3:J{}", "lst_Source")]
    for span, list_name in validations:
        dv = DataValidation(type="list", formula1=f"={list_name}", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(span.format(rows + 2))
    boolean = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=True)
    ws.add_data_validation(boolean)
    boolean.add(f"M3:M{rows + 2}")

    ws.conditional_formatting.add(
        f"U3:U{rows + 2}",
        CellIsRule(operator="equal", formula=['"Deadline passed"'],
                   fill=PatternFill("solid", fgColor="F8D7DA")))
    ws.conditional_formatting.add(
        f"T3:T{rows + 2}",
        CellIsRule(operator="between", formula=["0", "7"],
                   fill=PatternFill("solid", fgColor="FFF3CD")))

    ws.freeze_panes = "E3"
    ws.auto_filter.ref = f"A2:U{rows + 2}"
    return ws


def _build_dashboard(wb, tracker_rows: int) -> None:
    """Native-chart dashboard: funnel by round, plus outcome split and KPIs.

    Deliberately not a copy of the hand-built Sankey - that lives in the user's own
    workbook and append_into() preserves it. This is the standalone equivalent.
    """
    ws = wb.create_sheet("Dashboard")
    last = tracker_rows + 2

    ws["B2"] = "Placement Year Applications"
    ws["B2"].font = Font(bold=True, size=18, color=INK)
    ws["B3"] = "Live figures - they update as you fill in the Tracker."
    ws["B3"].font = Font(size=10, color="6B7280")

    kpis = [
        ("B5", "Roles logged", f'=COUNTA(Tracker!$A$3:$A${last})'),
        ("D5", "Applications sent", f'=COUNTIF(Tracker!$M$3:$M${last},TRUE)'),
        ("F5", "Still live", f'=SUMPRODUCT((Tracker!$A$3:$A${last}<>"")*'
                             f'(COUNTIF(lst_Closed,Tracker!$O$3:$O${last})=0)*'
                             f'(Tracker!$O$3:$O${last}<>"Not applied yet"))'),
        ("H5", "Closing in 7 days", f'=SUMPRODUCT((Tracker!$T$3:$T${last}<>"")*'
                                    f'(N(Tracker!$T$3:$T${last})>=0)*'
                                    f'(N(Tracker!$T$3:$T${last})<=7))'),
    ]
    for anchor, label, formula in kpis:
        col = anchor[0]
        ws[f"{col}5"] = label
        ws[f"{col}5"].font = Font(bold=True, size=9, color="6B7280")
        ws[f"{col}6"] = formula
        ws[f"{col}6"].font = Font(bold=True, size=26, color="1E3A5F")
        ws.column_dimensions[col].width = 18

    ws["B9"] = "HOW FAR EACH APPLICATION GOT"
    ws["B9"].font = Font(bold=True, size=11, color=INK)
    for i, stage in enumerate(LISTS["stg_Names"]):
        row = 10 + i
        ws[f"B{row}"] = stage
        # count anything that reached this round or beyond
        ws[f"C{row}"] = (f'=SUMPRODUCT((Tracker!$A$3:$A${last}<>"")*'
                         f'(IFERROR(MATCH(Tracker!$P$3:$P${last},stg_Names,0),0)>={i + 1}))')

    funnel = BarChart()
    funnel.type = "bar"
    funnel.title = "Applications by round reached"
    funnel.add_data(Reference(ws, min_col=3, min_row=10, max_row=14), titles_from_data=False)
    funnel.set_categories(Reference(ws, min_col=2, min_row=10, max_row=14))
    funnel.height, funnel.width = 7.5, 14
    funnel.legend = None
    ws.add_chart(funnel, "E9")

    ws["B17"] = "OUTCOMES"
    ws["B17"].font = Font(bold=True, size=11, color=INK)
    for i, outcome in enumerate(LISTS["out_Names"]):
        row = 18 + i
        ws[f"B{row}"] = outcome
        ws[f"C{row}"] = (f'=SUMPRODUCT((IFERROR(INDEX(st_Outcome,MATCH('
                         f'Tracker!$O$3:$O${last},st_Status,0)),"")="{outcome}")*1)')

    pie = PieChart()
    pie.title = "Outcome split"
    pie.add_data(Reference(ws, min_col=3, min_row=18, max_row=22), titles_from_data=False)
    pie.set_categories(Reference(ws, min_col=2, min_row=18, max_row=22))
    pie.height, pie.width = 7.5, 11
    ws.add_chart(pie, "E17")

    ws["B25"] = "WHAT NEEDS DOING"
    ws["B25"].font = Font(bold=True, size=11, color=INK)
    ws["B26"] = (f'=IFERROR("Next up: "&INDEX(Tracker!$A$3:$A${last},'
                 f'MATCH(MIN(IF(Tracker!$T$3:$T${last}<>"",'
                 f'IF(N(Tracker!$T$3:$T${last})>=0,N(Tracker!$T$3:$T${last})))),'
                 f'IF(Tracker!$T$3:$T${last}<>"",N(Tracker!$T$3:$T${last})),0)),'
                 f'"Nothing with a deadline yet")')
    ws["B26"].font = Font(size=11, color="1E3A5F")

    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 22


def export_new(jobs: list[dict], out_path: str | Path, rows: int = 300,
               home=None) -> dict:
    """Build a standalone tracker workbook containing these jobs."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _build_settings(wb, home)
    _build_tracker(wb, jobs, rows, home)
    _build_dashboard(wb, rows)
    wb._sheets = [wb["Tracker"], wb["Dashboard"], wb["Settings"]]
    wb.save(out_path)
    return {"added": len(jobs), "path": str(out_path), "rows_prepared": rows}
