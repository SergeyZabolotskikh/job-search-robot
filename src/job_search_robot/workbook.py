"""The master Excel workbook: results/job_leads.xlsx.

The tool owns every column except Status and Note, which belong to the user. Before each
run the tool reads Status/Note back into its memory, then rewrites the whole workbook.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .common import ConfigError
from .config import Settings

SHEET = "Leads"
STATUSES = ["new", "interested", "applied", "not relevant", "broken link", "duplicate"]
COLUMNS = [  # (header, width)
    ("Found on", 11), ("Status", 13), ("Note", 30), ("Track / CV", 18), ("Role family", 22),
    ("Title", 45), ("Company", 16), ("Location", 26), ("Work mode", 10), ("Posted", 11),
    ("Region", 14), ("Platform", 11), ("Page type", 11), ("Snippet", 60), ("Link", 40), ("Lead ID", 12),
]
HEADERS = [name for name, _ in COLUMNS]
NEW_FILL = PatternFill("solid", fgColor="E2F0D9")
OLD_FILL = PatternFill("solid", fgColor="FCE4D6")   # Posted more than old_posting_days ago


class WorkbookLockedError(ConfigError):
    pass


def ensure_writable(path: Path) -> None:
    """Fail before any paid search if Excel has the workbook open."""
    lock = path.with_name("~$" + path.name)
    if lock.exists():
        raise WorkbookLockedError(f"{path.name} is open in Excel. Close it and run again (no searches were made).")
    if path.exists():
        try:
            with path.open("r+b"):
                pass
        except PermissionError:
            raise WorkbookLockedError(f"{path.name} is locked by another program. Close it and run again "
                                      "(no searches were made).") from None


def read_notes(path: Path) -> dict[str, tuple[str, str]]:
    """Return {lead id: (status, note)} from the user's columns."""
    if not path.exists():
        return {}
    try:
        book = load_workbook(path, read_only=True)
    except Exception as exc:  # corrupt or not an xlsx: never overwrite it silently
        raise ConfigError(f"Cannot read {path.name} ({exc.__class__.__name__}). Fix or rename it; "
                          "the tool will not overwrite it.") from None
    try:
        sheet = book[SHEET] if SHEET in book.sheetnames else book.active
        rows = sheet.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(rows, ())]
        try:
            i_id, i_status, i_note = header.index("Lead ID"), header.index("Status"), header.index("Note")
        except ValueError:
            raise ConfigError(f"{path.name}: columns 'Lead ID', 'Status' and 'Note' are required. "
                              "Don't rename or delete them.") from None
        notes = {}
        for row in rows:
            if len(row) <= max(i_id, i_status, i_note) or not row[i_id]:
                continue
            notes[str(row[i_id])] = (str(row[i_status] or "").strip(), str(row[i_note] or "").strip())
        return notes
    finally:
        book.close()


def region_of(text: str, settings: Settings) -> str:
    """Tag a lead by city names in its title/snippet. Empty when no city is mentioned."""
    found, outside = [], []
    for region in settings.regions:
        if any(re.search(rf"(?<!\w){re.escape(city)}(?!\w)", text, re.I) for city in region.cities):
            (found if region.enabled else outside).append(region.name)
    if found:
        return ", ".join(found)
    return "outside area: " + ", ".join(outside) if outside else ""


def mentions_israel(text: str, settings: Settings) -> bool:
    """True when the text names Israel or a city from any region (enabled or not)."""
    if re.search(r"(?<!\w)israel(?!\w)", text, re.I):
        return True
    return any(re.search(rf"(?<!\w){re.escape(city)}(?!\w)", text, re.I)
               for region in settings.regions for city in region.cities)


def track_label(row: dict, settings: Settings) -> str:
    labels = []
    for track_id in row["tracks"]:
        track = settings.tracks.get(track_id)
        labels.append(track.label if track else track_id)
    return " + ".join(labels)


def family_label(row: dict, settings: Settings) -> str:
    names = {f.id: f.name for f in settings.families}
    return ", ".join(names.get(fid, fid) for fid in row["family_ids"])


def is_old(posted: str, days: int, today: date | None = None) -> bool:
    """True when a YYYY-MM-DD posting date is more than `days` days ago."""
    try:
        when = date.fromisoformat((posted or "")[:10])
    except ValueError:
        return False
    return ((today or date.today()) - when).days > days


def set_text(cell, value) -> None:
    """Write plain text: search results starting with '=' must never become formulas."""
    cell.value = value
    if isinstance(value, str) and value:
        cell.data_type = "s"


def write_workbook(path: Path, rows: list[dict], settings: Settings, new_ids: set[str]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = SHEET
    sheet.append(HEADERS)
    for index, (_, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
        sheet.cell(1, index).font = Font(bold=True)
    for r, row in enumerate(rows, start=2):
        check = row.get("check") or {}
        if check.get("status") == "closed":
            location = f'JOB CLOSED ({check.get("note")})'
        elif check.get("location"):
            location = check["location"]
        elif row["result_type"] != "job":
            location = ""
        else:
            location = "not verified" + (f' ({check["note"]})' if check.get("note") else "")
        region_text = check["location"] if check.get("location") else f'{row["title"]} {row["snippet"]}'
        region = region_of(region_text, settings)
        values = [row["first_seen_utc"][:10], row["status"], row["note"], track_label(row, settings),
                  family_label(row, settings), row["title"], row["company_url_slug"], location,
                  check.get("work_mode", ""), check.get("posted", ""), region, row["platform"],
                  row["result_type"].replace("_", " "), row["snippet"], row["url"], row["identity"]]
        for c, value in enumerate(values, start=1):
            set_text(sheet.cell(r, c), value)
        for c in (HEADERS.index("Title") + 1, HEADERS.index("Link") + 1):
            cell = sheet.cell(r, c)
            cell.hyperlink = row["url"]
            cell.font = Font(color="0563C1", underline="single")
        sheet.cell(r, HEADERS.index("Snippet") + 1).alignment = Alignment(wrap_text=False)
        if row["identity"] in new_ids:
            sheet.cell(r, 1).fill = NEW_FILL
        if is_old(check.get("posted", ""), settings.old_posting_days):
            sheet.cell(r, HEADERS.index("Posted") + 1).fill = OLD_FILL
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    validation = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True,
                                showErrorMessage=False)
    sheet.add_data_validation(validation)
    status_col = sheet.cell(1, HEADERS.index("Status") + 1).column_letter
    validation.add(f"{status_col}2:{status_col}{max(2, len(rows) + 500)}")
    _save_atomic(book, path)


def _save_atomic(book: Workbook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".xlsx")
    os.close(descriptor)
    try:
        book.save(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
