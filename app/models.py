"""Internal, source-agnostic representation of one upstream record."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Record:
    """A cleaned record from either upstream system.

    ``raw`` is always kept untouched so the UI can show exactly what the source said.
    Every cleaning step that changed or could not use a value adds an entry to
    ``warnings`` so nothing is silently "fixed".
    """

    source: str                      # "crm" | "calendar"
    id: str
    raw: dict

    title: Optional[str] = None
    date: Optional[dt.date] = None   # local calendar date (None if unusable)
    start: Optional[dt.datetime] = None   # tz-aware, LOCAL_TZ. None if time unknown
    end: Optional[dt.datetime] = None
    location: Optional[str] = None
    mode: Optional[str] = None       # "virtual" | "in_person" | None (unknown / n.a.)
    is_internal: Optional[bool] = None
    status: Optional[str] = None     # canonical: confirmed | tentative | cancelled | completed
    status_raw: Optional[str] = None
    notes: Optional[str] = None
    recurring: Optional[bool] = None
    created_at: Optional[dt.datetime] = None

    # calendar-side
    attendees: list = field(default_factory=list)            # valid, lower-cased emails
    externals: list = field(default_factory=list)            # subset of attendees outside INTERNAL_DOMAIN
    placeholder_attendees: list = field(default_factory=list)  # entries that are not email addresses
    raw_attendee_count: int = 0
    organizer: Optional[str] = None
    start_was_utc: bool = False

    # crm-side
    client_name: Optional[str] = None
    client_company: Optional[str] = None
    owner: Optional[str] = None
    meeting_type_raw: Optional[str] = None

    # bookkeeping
    warnings: list = field(default_factory=list)
    merged_ids: list = field(default_factory=list)    # same-source duplicates folded into this record
    merged_raw: list = field(default_factory=list)
    alternates: list = field(default_factory=list)    # values the folded duplicates disagreed on
    dup_reasons: list = field(default_factory=list)
