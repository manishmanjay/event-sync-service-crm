"""Ingestion: load the two JSON files and turn each raw dict into a clean ``Record``.

Principles
* Never crash on one bad record - repair it if the intent is unambiguous, otherwise
  keep the field empty. Either way, record a warning saying what happened.
* Never throw information away: the untouched raw record travels with the Record.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Optional

from .config import INTERNAL_DOMAIN, LOCAL_TZ, LOCAL_TZ_NAME
from .models import Record

EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
VIRTUAL_HINTS = ("zoom", "teams", "virtual", "webex", "google meet", "meet.google", "hangout", "skype")
PLACEHOLDER_NAMES = {"multiple", "various", "tbd", "tba", "n/a", "na", "unknown", "none"}

CRM_STATUS = {
    "completed": "completed",
    "confirmed": "confirmed",
    "scheduled": "confirmed",   # "Scheduled" and "Confirmed" both mean "on the books"
    "tentative": "tentative",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
CAL_STATUS = {"confirmed": "confirmed", "tentative": "tentative", "cancelled": "cancelled", "canceled": "cancelled"}


# --------------------------------------------------------------------------- helpers
def _warn(rec: Record, field: str, kind: str, message: str) -> None:
    rec.warnings.append(
        {"source": rec.source, "record_id": rec.id, "field": field, "kind": kind, "message": message}
    )


def _clean(value: Any) -> Optional[str]:
    """Strip strings; treat empty / whitespace-only as missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def infer_mode(location: Optional[str]) -> Optional[str]:
    if not location:
        return None
    low = location.lower()
    return "virtual" if any(h in low for h in VIRTUAL_HINTS) else "in_person"


def name_from_email(email: str) -> str:
    local = email.split("@")[0]
    return " ".join(p.capitalize() for p in re.split(r"[._-]+", local) if p)


def _parse_iso(value: Any) -> tuple[Optional[dt.datetime], bool]:
    """Parse an ISO timestamp into LOCAL_TZ. Zoneless values are *assumed* local.

    Returns (datetime | None, had_explicit_offset).
    """
    if not isinstance(value, str) or not value.strip():
        return None, False
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ), False
    return parsed.astimezone(LOCAL_TZ), True


def _parse_crm_date(value: Any) -> tuple[Optional[dt.date], Optional[str]]:
    """Parse the CRM's date. Returns (date | None, note about any repair/assumption)."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    s = value.strip()
    try:
        return dt.date.fromisoformat(s), None
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)
    if not m:
        return None, None
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if a > 12 and b <= 12:                      # unambiguous day-first
            return dt.date(year, b, a), f"'{s}' is not ISO; read as DD-MM-YYYY"
        if a <= 12 and b > 12:                      # unambiguous month-first
            return dt.date(year, a, b), f"'{s}' is not ISO; read as MM-DD-YYYY"
        if a <= 12 and b <= 12:                     # ambiguous: US month-first assumed
            return dt.date(year, a, b), f"'{s}' is not ISO and ambiguous; assumed MM-DD-YYYY"
    except ValueError:
        return None, None
    return None, None


def _parse_hhmm(value: Any) -> Optional[dt.time]:
    if not isinstance(value, str):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return dt.time(h, mi) if h < 24 and mi < 60 else None


# --------------------------------------------------------------------------- CRM
def normalize_crm(raw: Any) -> Record:
    if not isinstance(raw, dict):
        raise ValueError("record is not a JSON object")
    rid = _clean(raw.get("crm_id"))
    if not rid:
        raise ValueError("missing crm_id")
    rec = Record(source="crm", id=rid, raw=raw)

    rec.title = _clean(raw.get("subject"))
    rec.client_name = _clean(raw.get("client_name"))
    rec.client_company = _clean(raw.get("client_company"))
    rec.owner = _clean(raw.get("relationship_owner"))
    rec.location = _clean(raw.get("location"))
    rec.notes = _clean(raw.get("notes"))
    rec.meeting_type_raw = _clean(raw.get("meeting_type"))
    rec.created_at = _parse_iso(raw.get("created_at"))[0]

    # date + time ------------------------------------------------------------
    rec.date, note = _parse_crm_date(raw.get("meeting_date"))
    if rec.date is None:
        _warn(rec, "meeting_date", "malformed",
              f"meeting_date {raw.get('meeting_date')!r} could not be parsed; record cannot be matched by date")
    elif note:
        _warn(rec, "meeting_date", "repaired", note)

    t = _parse_hhmm(raw.get("meeting_time"))
    if raw.get("meeting_time") in (None, ""):
        _warn(rec, "meeting_time", "missing", "CRM has no meeting_time; time must come from the calendar")
    elif t is None:
        _warn(rec, "meeting_time", "malformed", f"meeting_time {raw.get('meeting_time')!r} could not be parsed")
    if rec.date and t:
        rec.start = dt.datetime.combine(rec.date, t, tzinfo=LOCAL_TZ)

    # type / mode ------------------------------------------------------------
    mt = (rec.meeting_type_raw or "").lower()
    rec.mode = {"in-person": "in_person", "in person": "in_person", "virtual": "virtual"}.get(mt)
    rec.is_internal = True if mt == "internal" else (False if mt else None)
    if not rec.meeting_type_raw:
        _warn(rec, "meeting_type", "missing", "meeting_type is empty")

    # status -----------------------------------------------------------------
    rec.status_raw = _clean(raw.get("status"))
    rec.status = CRM_STATUS.get((rec.status_raw or "").lower())
    if rec.status_raw and rec.status is None:
        _warn(rec, "status", "malformed", f"unrecognised status {rec.status_raw!r}")

    # empties / placeholders ---------------------------------------------------
    if "location" in raw and rec.location is None:
        _warn(rec, "location", "missing", "location is empty")
    if "notes" in raw and raw.get("notes") is not None and rec.notes is None:
        _warn(rec, "notes", "missing", "notes is an empty string; treated as missing")
    if rec.client_name and rec.client_name.lower() in PLACEHOLDER_NAMES:
        _warn(rec, "client_name", "placeholder",
              f"client_name {rec.client_name!r} is a placeholder, not a person; not used for matching")
    if rec.client_name is None and rec.client_company is None and rec.is_internal is False:
        _warn(rec, "client_name", "missing", "external meeting has no client recorded")
    return rec


# --------------------------------------------------------------------------- calendar
def normalize_calendar(raw: Any) -> Record:
    if not isinstance(raw, dict):
        raise ValueError("record is not a JSON object")
    rid = _clean(raw.get("event_id"))
    if not rid:
        raise ValueError("missing event_id")
    rec = Record(source="calendar", id=rid, raw=raw)

    rec.title = _clean(raw.get("title"))
    rec.location = _clean(raw.get("location"))
    rec.mode = infer_mode(rec.location)
    rec.notes = _clean(raw.get("description"))
    organizer = _clean(raw.get("organizer"))
    rec.organizer = organizer.lower() if organizer else None
    rec.recurring = raw.get("is_recurring") if isinstance(raw.get("is_recurring"), bool) else None
    rec.created_at = _parse_iso(raw.get("created_at"))[0]

    # times ------------------------------------------------------------------
    rec.start, rec.start_was_utc = _parse_iso(raw.get("start_time"))
    rec.end, _ = _parse_iso(raw.get("end_time"))
    if rec.start is None:
        _warn(rec, "start_time", "malformed", f"start_time {raw.get('start_time')!r} could not be parsed")
    else:
        rec.date = rec.start.date()
        if rec.start_was_utc:
            _warn(rec, "start_time", "normalised",
                  f"start_time {raw.get('start_time')!r} is UTC while other timestamps are zoneless; "
                  f"converted to {LOCAL_TZ_NAME} -> {rec.start.strftime('%H:%M %Z')}")
    end_raw = raw.get("end_time")
    if isinstance(end_raw, str) and re.search(r"T\d{2}:\d{2}$", end_raw.strip()):
        _warn(rec, "end_time", "repaired", f"end_time {end_raw!r} has no seconds; parsed as {end_raw.strip()}:00")
    if rec.end is None and end_raw:
        _warn(rec, "end_time", "malformed", f"end_time {end_raw!r} could not be parsed")
    if rec.start and rec.end and rec.end <= rec.start:
        _warn(rec, "end_time", "malformed", "end_time is not after start_time")

    # attendees ---------------------------------------------------------------
    attendees_raw = raw.get("attendees") or []
    rec.raw_attendee_count = len(attendees_raw)
    for a in attendees_raw:
        original = str(a).strip()
        fixed = original.lower().replace("[at]", "@").replace("(at)", "@")
        if EMAIL_RE.match(fixed):
            if fixed != original.lower():
                _warn(rec, "attendees", "repaired", f"attendee {original!r} is not a valid address; read as {fixed!r}")
            if fixed not in rec.attendees:
                rec.attendees.append(fixed)
        else:
            rec.placeholder_attendees.append(original)
            _warn(rec, "attendees", "placeholder",
                  f"attendee {original!r} is not an email address; kept as a placeholder, not counted as a person")
    rec.externals = [e for e in rec.attendees if not e.endswith("@" + INTERNAL_DOMAIN)]
    if rec.attendees or rec.placeholder_attendees:
        rec.is_internal = not rec.externals and not rec.placeholder_attendees
    if not attendees_raw:
        _warn(rec, "attendees", "missing", "attendee list is empty")

    # status ------------------------------------------------------------------
    rec.status_raw = _clean(raw.get("status"))
    rec.status = CAL_STATUS.get((rec.status_raw or "").lower())
    if rec.status_raw and rec.status is None:
        _warn(rec, "status", "malformed", f"unrecognised status {rec.status_raw!r}")

    if "location" in raw and rec.location is None:
        _warn(rec, "location", "missing", "location is empty")
    if "description" in raw and rec.notes is None:
        _warn(rec, "description", "missing", "description is empty")
    return rec


# --------------------------------------------------------------------------- loading
def _load_json(path: Path) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Data file not found: {path}. Set DATA_DIR or place the file there.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"{path.name} must contain a JSON array of records")
    return data


def load_records(data_dir: Path) -> tuple[list, list, list]:
    """Return (crm_records, calendar_records, rejected). One bad record never aborts the run."""
    crm, cal, rejected = [], [], []
    for source, filename, fn, out in (
        ("crm", "crm_events.json", normalize_crm, crm),
        ("calendar", "calendar_events.json", normalize_calendar, cal),
    ):
        seen: set = set()
        for index, raw in enumerate(_load_json(Path(data_dir) / filename)):
            try:
                rec = fn(raw)
                if rec.id in seen:
                    raise ValueError(f"duplicate id {rec.id!r} in file")
                seen.add(rec.id)
                out.append(rec)
            except ValueError as exc:
                rejected.append({"source": source, "index": index, "reason": str(exc), "raw": raw})
    return crm, cal, rejected
