"""Build the unified meeting list: pair up records, then resolve every field.

Field policy (also documented in the README)
  scheduling facts (start, end, location, attendees, recurrence)  -> calendar wins
  relationship facts (client, company, owner, meeting type, notes) -> CRM wins
  title                                                            -> calendar (more descriptive)
  status                                                           -> "cancelled" beats everything
A value is only ever taken from the other source when the preferred source has none.
Whenever both sources have a value and they disagree, a *conflict* is recorded - the
resolved value is a best guess, the conflict is how a human knows to double-check it.

Per-field `state` values:
  match         both sources agree
  compatible    both present, no contradiction (e.g. "NYC Office" vs "NYC Office - 12th Floor")
  differs       both present and different, but not a contradiction by design (titles, notes, owner vs organizer)
  conflict      both present and contradictory
  only_crm / only_calendar   value exists on one side only
  missing       neither side has it
  n/a           not applicable (e.g. "client" for an internal meeting)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import Counter
from typing import Optional

from .config import DATA_DIR, LOCAL_TZ_NAME, MATCH_THRESHOLD
from .matching import (
    Match, closest_candidate, dedupe, locations_compatible, match_sources, more_specific_location,
    name_matches_attendee,
)
from .models import Record
from .normalize import PLACEHOLDER_NAMES, load_records, name_from_email

MODE_LABEL = {"virtual": "Virtual", "in_person": "In-Person"}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _fmt(d: Optional[dt.datetime]) -> Optional[str]:
    return d.strftime("%Y-%m-%d %H:%M") if d else None


def _field(name, label, crm, cal, resolved, source, state, detail=None) -> dict:
    return {"name": name, "label": label, "crm": crm, "calendar": cal,
            "resolved": resolved, "resolved_from": source, "state": state, "detail": detail}


def _only_state(crm_v, cal_v) -> str:
    if crm_v is not None and cal_v is None:
        return "only_crm"
    if cal_v is not None and crm_v is None:
        return "only_calendar"
    return "missing"


def _conflict(conflicts: list, field: dict, severity: str, explanation: str) -> None:
    field["state"] = "conflict"
    field["detail"] = explanation
    conflicts.append({
        "field": field["name"], "label": field["label"], "severity": severity,
        "crm": field["crm"], "calendar": field["calendar"], "resolved": field["resolved"],
        "resolved_from": field["resolved_from"], "explanation": explanation,
    })


# --------------------------------------------------------------------------- per-field resolvers
def _title(crm, cal) -> dict:
    c, k = (crm.title if crm else None), (cal.title if cal else None)
    if c and k:
        same = c.casefold() == k.casefold()
        return _field("title", "Title", c, k, k, "both" if same else "calendar",
                      "match" if same else "differs",
                      None if same else "Titles are written independently in each system; the calendar title is shown.")
    if k:
        return _field("title", "Title", None, k, k, "calendar", "only_calendar")
    company = crm.client_company if crm else None
    label = c if not company or (c and company.split()[0].lower() in c.lower()) else f"{c} - {company}"
    return _field("title", "Title", c, None, label, "crm", "only_crm")


def _start(crm, cal, conflicts) -> dict:
    crm_disp = None
    if crm and crm.date:
        crm_disp = _fmt(crm.start) if crm.start else f"{crm.date.isoformat()} (no time recorded)"
    cal_disp = _fmt(cal.start) if cal else None

    if crm and cal and crm.start and cal.start:
        diff = int((cal.start - crm.start).total_seconds() // 60)
        if diff == 0:
            return _field("start", "Start", crm_disp, cal_disp, cal_disp, "both", "match")
        f = _field("start", "Start", crm_disp, cal_disp, cal_disp, "calendar", "conflict")
        direction = "later" if diff > 0 else "earlier"
        text = (f"CRM says {crm_disp}, calendar says {cal_disp} (calendar is {abs(diff)} min {direction}). "
                f"Using the calendar time because it is what attendees were actually invited to.")
        if cal.start_was_utc and abs(diff) == 60:
            text += (f" Note: the calendar timestamp is stored in UTC. Converted to {LOCAL_TZ_NAME} it falls in "
                     f"daylight time on that date; the two systems would agree exactly if the CRM time had been "
                     f"entered as standard time. Likely an offset assumption, not a rescheduled meeting.")
        for alt in cal.alternates:
            if alt["start"] == crm_disp:
                text += f" The duplicate calendar entry {alt['id']} does start at {alt['start']}, matching the CRM."
        _conflict(conflicts, f, "high" if abs(diff) > 60 else "medium", text)
        return f
    if crm and cal and cal.start:      # CRM has a date but no usable time
        return _field("start", "Start", crm_disp, cal_disp, cal_disp, "calendar", "only_calendar",
                      "CRM has the date but no time; the time comes from the calendar.")
    if cal and cal.start:
        return _field("start", "Start", None, cal_disp, cal_disp, "calendar", "only_calendar")
    if crm and crm.start:
        return _field("start", "Start", crm_disp, None, crm_disp, "crm", "only_crm")
    return _field("start", "Start", crm_disp, cal_disp, crm_disp, "crm" if crm_disp else None, "missing")


def _end(crm, cal) -> dict:
    k = _fmt(cal.end) if cal else None
    if k:
        return _field("end", "End", None, k, k, "calendar", "only_calendar", "The CRM stores no end time.")
    return _field("end", "End", None, None, None, None, "missing", "The CRM stores no end time.")


def _location(crm, cal, conflicts) -> dict:
    c = crm.location if crm else None
    k = cal.location if cal else None
    # A merged duplicate may carry a more specific form of the same location (e.g. a room number).
    k_eff, dup_note = k, None
    if cal and k:
        for alt in cal.alternates:
            a = alt.get("location")
            if a and locations_compatible(k_eff, a) and more_specific_location(k_eff, a) == a and a != k_eff:
                k_eff = a
                dup_note = f"The merged duplicate entry {alt['id']} has the more specific location '{a}', which is used."
    if c and k:
        if c.casefold() == k.casefold() and k_eff == k:
            return _field("location", "Location", c, k, k, "both", "match")
        if locations_compatible(c, k_eff):
            best = more_specific_location(c, k_eff)
            detail = "One system is just more specific than the other; the more specific value is shown."
            return _field("location", "Location", c, k, best, "crm" if best == c else "calendar", "compatible",
                          f"{dup_note} {detail}" if dup_note else detail)
        f = _field("location", "Location", c, k, k, "calendar", "conflict")
        _conflict(conflicts, f, "medium",
                  f"CRM location '{c}' and calendar location '{k}' describe different places. "
                  f"Using the calendar value.")
        return f
    if k:
        return _field("location", "Location", None, k, k, "calendar", "only_calendar")
    if c:
        return _field("location", "Location", c, None, c, "crm", "only_crm")
    return _field("location", "Location", None, None, None, None, "missing")


def _format(crm, cal, conflicts) -> dict:
    c_raw = crm.meeting_type_raw if crm else None
    c_mode = crm.mode if crm else None
    k_mode = cal.mode if cal else None
    k_disp = f"{MODE_LABEL[k_mode]} (inferred from location)" if k_mode else None
    if c_mode and k_mode:
        if c_mode == k_mode:
            return _field("format", "Format", c_raw, k_disp, MODE_LABEL[c_mode], "both", "match")
        f = _field("format", "Format", c_raw, k_disp, MODE_LABEL[k_mode], "calendar", "conflict")
        _conflict(conflicts, f, "high",
                  f"CRM says {c_raw}; the calendar location '{cal.location}' implies {MODE_LABEL[k_mode]}. "
                  f"People may turn up in the wrong place. Showing the calendar's format; confirm with the owner.")
        return f
    if c_raw and not c_mode and k_mode:     # CRM "Internal" is a category, not a format
        return _field("format", "Format", c_raw, k_disp, MODE_LABEL[k_mode], "calendar", "compatible",
                      f"CRM classifies this as '{c_raw}', which is a category rather than a format.")
    if k_mode:
        return _field("format", "Format", c_raw, k_disp, MODE_LABEL[k_mode], "calendar", "only_calendar",
                      "CRM has no meeting type." if not c_raw else None)
    if c_raw:
        return _field("format", "Format", c_raw, None, MODE_LABEL.get(c_mode, c_raw), "crm", "only_crm",
                      "Calendar has no location to infer a format from." if cal else None)
    return _field("format", "Format", None, None, None, None, "missing")


def _status(crm, cal, conflicts) -> dict:
    c, k = (crm.status if crm else None), (cal.status if cal else None)
    c_raw, k_raw = (crm.status_raw if crm else None), (cal.status_raw if cal else None)
    if c and k:
        if c == k:
            return _field("status", "Status", c_raw, k_raw, c, "both", "match")
        if "cancelled" in (c, k):
            who = "crm" if c == "cancelled" else "calendar"
            f = _field("status", "Status", c_raw, k_raw, "cancelled", who, "conflict")
            _conflict(conflicts, f, "high",
                      f"CRM status is '{c_raw}' but the calendar status is '{k_raw}'. One system says the meeting "
                      f"is off, the other still shows it on. Resolved to 'cancelled' (an explicit cancellation is "
                      f"more likely than a stale cancellation); the calendar entry probably needs cleaning up.")
            return f
        if {c, k} <= {"confirmed", "completed"}:
            return _field("status", "Status", c_raw, k_raw, "completed", "crm" if c == "completed" else "calendar",
                          "compatible", "'completed' and 'confirmed' do not contradict each other.")
        f = _field("status", "Status", c_raw, k_raw, "tentative", "crm" if c == "tentative" else "calendar", "conflict")
        _conflict(conflicts, f, "medium",
                  f"CRM status '{c_raw}' vs calendar status '{k_raw}'. Resolved to the less certain value.")
        return f
    if c:
        return _field("status", "Status", c_raw, None, c, "crm", "only_crm")
    if k:
        return _field("status", "Status", None, k_raw, k, "calendar", "only_calendar")
    return _field("status", "Status", None, None, None, None, "missing")


def _client(crm, cal) -> dict:
    label = None
    if crm and (crm.client_name or crm.client_company):
        label = crm.client_name or ""
        if crm.client_company:
            label = f"{label} ({crm.client_company})" if label else crm.client_company
    externals = cal.externals if cal else []
    cal_val = ", ".join(externals) if externals else None
    internal = (crm and crm.is_internal) or (cal and cal.is_internal)
    if not label and not cal_val:
        return _field("client", "Client", None, None, None, None, "n/a" if internal else "missing",
                      "Internal meeting - no external client." if internal else None)
    if label and cal_val:
        placeholder = crm.client_name and crm.client_name.lower() in PLACEHOLDER_NAMES
        hit =crm.client_name and name_matches_attendee(crm.client_name, externals)
        if hit:
            return _field("client", "Client", label, cal_val, label, "both", "match",
                          "CRM client appears among the external calendar attendees.")
        if placeholder:
            return _field("client", "Client", label, cal_val, label, "crm", "compatible",
                          "CRM records a group placeholder; the calendar lists the individual attendees.")
        return _field("client", "Client", label, cal_val, label, "crm", "differs",
                      "CRM client not found among the calendar's external attendees.")
    if label:
        return _field("client", "Client", label, None, label, "crm", "only_crm")
    return _field("client", "Client", None, cal_val, cal_val, "calendar", "only_calendar",
                  "No client in the CRM; derived from external attendees.")


def _attendees(crm, cal, conflicts) -> dict:
    people = (list(cal.attendees) + list(cal.placeholder_attendees)) if cal else None
    if not cal:
        who = crm.client_name if crm and crm.client_name else None
        return _field("attendees", "Attendees", None, None, None, None, "missing",
                      "No calendar entry, so the invitee list is unknown." +
                      (f" The CRM names only the primary contact ({who})." if who else ""))
    detail = "The CRM stores only the primary contact, not an attendee list."
    if cal.merged_ids:
        detail = f"Union of the attendees on {cal.id} and {', '.join(cal.merged_ids)}. " + detail
    f = _field("attendees", "Attendees", None, people or None, people or None, "calendar",
               "only_calendar" if people else "missing", detail)
    if crm and crm.notes:
        m = re.search(r"\b(\d{1,3})\s+attendees?\b", crm.notes, re.I)
        listed = len(cal.attendees) + len(cal.placeholder_attendees)
        if m and int(m.group(1)) != listed:
            claimed = int(m.group(1))
            f["crm"] = f"{claimed} (from CRM notes)"
            _conflict(conflicts, f, "low",
                      f"CRM notes say {claimed} attendees are confirmed, but the calendar lists {listed} "
                      f"({len(cal.attendees)} email addresses"
                      f"{', plus a non-email placeholder ' + repr(cal.placeholder_attendees[0]) if cal.placeholder_attendees else ''}). "
                      f"The calendar invite list is probably incomplete.")
    return f


def _owner(crm, cal) -> dict:
    c = crm.owner if crm else None
    k = name_from_email(cal.organizer) if cal and cal.organizer else None
    if c and k:
        if c.casefold() == k.casefold():
            return _field("owner", "Owner / organizer", c, k, c, "both", "match")
        return _field("owner", "Owner / organizer", c, k, c, "crm", "differs",
                      "Relationship owner and calendar organizer are different roles, so this is not a conflict "
                      "(e.g. an assistant may organise on behalf of the owner). CRM owner is shown.")
    if c:
        return _field("owner", "Owner / organizer", c, None, c, "crm", "only_crm")
    if k:
        return _field("owner", "Owner / organizer", None, k, k, "calendar", "only_calendar")
    return _field("owner", "Owner / organizer", None, None, None, None, "missing")


def _recurring(cal) -> dict:
    if cal and cal.recurring is not None:
        v = "Yes" if cal.recurring else "No"
        return _field("recurring", "Recurring", None, v, v, "calendar", "only_calendar",
                      "Recurrence exists only in the calendar. Each occurrence is its own meeting.")
    return _field("recurring", "Recurring", None, None, None, None, "missing")


def _notes(crm, cal) -> dict:
    c, k = (crm.notes if crm else None), (cal.notes if cal else None)
    if c and k:
        return _field("notes", "Notes / description", c, k, None, "both", "differs",
                      "Free text written separately in each system; both are shown.")
    return _field("notes", "Notes / description", c, k, c or k, "crm" if c else ("calendar" if k else None),
                  _only_state(c, k))


# --------------------------------------------------------------------------- one meeting
def _meeting_id(crm: Optional[Record], cal: Optional[Record]) -> str:
    ids = sorted(([crm.id] + crm.merged_ids if crm else []) + ([cal.id] + cal.merged_ids if cal else []))
    return "mtg-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:8]


def build_meeting(crm: Optional[Record], cal: Optional[Record], match: Optional[Match], closest=None) -> dict:
    conflicts: list = []
    fields = [
        _title(crm, cal),
        _start(crm, cal, conflicts),
        _end(crm, cal),
        _location(crm, cal, conflicts),
        _format(crm, cal, conflicts),
        _status(crm, cal, conflicts),
        _client(crm, cal),
        _attendees(crm, cal, conflicts),
        _owner(crm, cal),
        _recurring(cal),
        _notes(crm, cal),
    ]
    by_name = {f["name"]: f for f in fields}
    presence = "both" if crm and cal else ("crm_only" if crm else "calendar_only")

    anchor = cal or crm
    start = (cal.start if cal and cal.start else (crm.start if crm else None))
    warnings = (crm.warnings if crm else []) + (cal.warnings if cal else [])
    duplicates = []
    for r in (crm, cal):
        if r and r.merged_ids:
            duplicates.append({"source": r.source, "primary": r.id, "merged": r.merged_ids, "reasons": r.dup_reasons})

    return {
        "id": _meeting_id(crm, cal),
        "title": by_name["title"]["resolved"],
        "date": anchor.date.isoformat() if anchor.date else None,
        "start": start.isoformat() if start else None,
        "start_local": _fmt(start),
        "end_local": by_name["end"]["resolved"],
        "time_known": bool(start),
        "presence": presence,
        "source_ids": {
            "crm": ([crm.id] + crm.merged_ids) if crm else [],
            "calendar": ([cal.id] + cal.merged_ids) if cal else [],
        },
        "match": ({"score": match.score, "reasons": match.reasons} if match else
                  {"score": None, "reasons": [], "closest_candidate": closest}),
        "status": by_name["status"]["resolved"],
        "client": by_name["client"]["resolved"],
        "owner": by_name["owner"]["resolved"],
        "format": by_name["format"]["resolved"],
        "location": by_name["location"]["resolved"],
        "conflict_count": len(conflicts),
        "max_severity": max((c["severity"] for c in conflicts), key=SEVERITY_RANK.get) if conflicts else None,
        "conflict_fields": [c["field"] for c in conflicts],
        "warning_count": len(warnings),
        "fields": fields,
        "conflicts": conflicts,
        "warnings": warnings,
        "duplicates": duplicates,
        "raw": {
            "crm": ([crm.raw] + crm.merged_raw) if crm else [],
            "calendar": ([cal.raw] + cal.merged_raw) if cal else [],
        },
    }


# --------------------------------------------------------------------------- whole data set
def reconcile_all(data_dir=DATA_DIR) -> dict:
    crm_all, cal_all, rejected = load_records(data_dir)
    crm = dedupe(crm_all)
    cal = dedupe(cal_all)
    matches, crm_only, cal_only = match_sources(crm, cal)

    meetings = [build_meeting(m.crm, m.cal, m) for m in matches]
    meetings += [build_meeting(c, None, None, closest_candidate(c, cal)) for c in crm_only]
    meetings += [build_meeting(None, k, None, closest_candidate(k, crm)) for k in cal_only]
    meetings.sort(key=lambda m: (m["date"] or "9999-99-99", (m["start_local"] or "99")[11:] or "99", m["id"]))

    conflict_fields = Counter(f for m in meetings for f in m["conflict_fields"])
    warn_kinds = Counter(w["kind"] for m in meetings for w in m["warnings"])
    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "timezone": LOCAL_TZ_NAME,
        "match_threshold": MATCH_THRESHOLD,
        "counts": {
            "crm_records": len(crm_all),
            "calendar_records": len(cal_all),
            "meetings": len(meetings),
            "both": sum(m["presence"] == "both" for m in meetings),
            "crm_only": sum(m["presence"] == "crm_only" for m in meetings),
            "calendar_only": sum(m["presence"] == "calendar_only" for m in meetings),
            "with_conflicts": sum(m["conflict_count"] > 0 for m in meetings),
            "conflicts_total": sum(m["conflict_count"] for m in meetings),
            "with_warnings": sum(m["warning_count"] > 0 for m in meetings),
            "duplicates_merged": sum(len(r.merged_ids) for r in crm + cal),
            "rejected": len(rejected),
        },
        "conflicts_by_field": dict(conflict_fields),
        "warnings_by_kind": dict(warn_kinds),
        "rejected": rejected,
        "assumptions": [
            f"Zoneless timestamps are interpreted as {LOCAL_TZ_NAME}; UTC ('Z') timestamps are converted to it.",
            "Records only match when they fall on the same local date.",
            f"Pairs scoring >= {MATCH_THRESHOLD:.2f} are treated as the same meeting (one-to-one, best first).",
            "Scheduling facts (time, location, attendees) prefer the calendar; relationship facts prefer the CRM.",
        ],
    }
    return {"meetings": meetings, "summary": summary}
