"""Deciding which records describe the same real-world meeting.

Two separate problems, solved in this order:

1. Within-source de-duplication  (e.g. two calendar entries for one Pinnacle meeting)
2. Cross-source matching         (CRM record <-> calendar entry)

Doing (1) first matters: otherwise two near-identical calendar entries compete for the
same CRM record and one of them is wrongly left "calendar only".

Matching is a transparent weighted score, not a black box. Each signal has a weight;
signals that cannot be evaluated for a pair (e.g. the CRM has no time) are left out of
the denominator rather than counted as a mismatch. The score is therefore
"how much of the *available* evidence agrees", and every contributing reason is returned
so the UI can explain why two records were joined.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .config import INTERNAL_DOMAIN, MATCH_THRESHOLD
from .models import Record
from .normalize import PLACEHOLDER_NAMES

# --------------------------------------------------------------------------- text helpers
STOP = {"the", "a", "an", "and", "of", "with", "for", "re", "vs", "meeting", "call", "session", "w"}
# Abbreviations that appear in one system but are spelled out in the other.
ALIASES = {
    "lpac": "lp advisory committee",
    "qbr": "quarterly business review",
    "dd": "due diligence",
}
# Words that describe *kinds* of organisation and so say nothing about *which* one.
GENERIC_ORG_WORDS = {
    "capital", "partners", "group", "holdings", "investments", "investors", "ventures", "wealth",
    "advisors", "advisory", "institutional", "fund", "management", "asset", "global", "inc", "llc",
}
LOCATION_STOP = {"the", "restaurant"}

W = {"time": 0.35, "person": 0.30, "company": 0.20, "title": 0.20, "location": 0.05, "internal": 0.10}


def tokens(text: Optional[str]) -> set:
    if not text:
        return set()
    low = re.sub(r"https?://\S+", " ", text.lower())
    for short, long in ALIASES.items():
        low = re.sub(rf"\b{short}\b", long, low)
    return {t for t in re.findall(r"[a-z0-9]+", low) if t not in STOP}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def location_tokens(loc: Optional[str]) -> set:
    if not loc:
        return set()
    return {t for t in re.findall(r"[a-z0-9]+", re.sub(r"https?://\S+", " ", loc.lower())) if t not in LOCATION_STOP}


def locations_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """True if one location is the same as, or a less-specific form of, the other.

    "NYC Office" ~ "NYC Office - 12th Floor";  "Zoom" ~ "Zoom - https://zoom.us/j/1";
    "Conference Room A" !~ "Conference Room B";  "NYC Office - 30th Floor" !~ "Zoom".
    """
    ta, tb = location_tokens(a), location_tokens(b)
    return bool(ta and tb and (ta <= tb or tb <= ta))


def more_specific_location(a: str, b: str) -> str:
    return a if len(location_tokens(a)) > len(location_tokens(b)) else b


def _name_parts(name: str) -> list:
    return re.findall(r"[a-z]+", name.lower())


def name_matches_attendee(name: str, emails: list) -> bool:
    parts = _name_parts(name)
    if len(parts) < 2:
        return False
    first, last = parts[0], parts[-1]
    for email in emails:
        local = re.sub(r"[^a-z]", "", email.split("@")[0])
        if first in local and last in local:
            return True
    return False


def company_hit(company: str, cal: Record) -> Optional[bool]:
    """Does the CRM company show up in the calendar entry's external domains/title/description?

    Returns None when the company name has no distinguishing word (e.g. "Fund VII Investors").
    """
    sig = [t for t in re.findall(r"[a-z]+", company.lower()) if len(t) >= 4 and t not in GENERIC_ORG_WORDS]
    if not sig:
        return None
    domains = " ".join(e.split("@")[1] for e in cal.externals)
    blob = f"{cal.title or ''} {cal.notes or ''}".lower()
    return any(t in domains or t in blob for t in sig)


# --------------------------------------------------------------------------- scoring
def _time_fraction(diff_min: float) -> float:
    if diff_min == 0:
        return 1.0
    if diff_min <= 15:
        return 0.85
    if diff_min <= 60:
        return 0.70
    if diff_min <= 120:
        return 0.45
    if diff_min <= 240:
        return 0.15
    return 0.0


def score_pair(crm: Record, cal: Record) -> tuple[float, list]:
    """Return (score 0..1, human-readable reasons). Different local dates never match."""
    if crm.date is None or crm.date != cal.date:
        return 0.0, []

    got = avail = 0.0
    reasons = [f"same date ({crm.date.isoformat()})"]

    def add(name: str, frac: float, reason: str) -> None:
        nonlocal got, avail
        got += W[name] * frac
        avail += W[name]
        if frac > 0:
            reasons.append(reason)

    if crm.start and cal.start:
        diff = abs((crm.start - cal.start).total_seconds()) / 60
        add("time", _time_fraction(diff),
            "identical start time" if diff == 0 else f"start times {diff:.0f} min apart")
    else:
        reasons.append("CRM has no time, so time was not used")

    if crm.client_name and crm.client_name.lower() not in PLACEHOLDER_NAMES:
        hit = name_matches_attendee(crm.client_name, cal.externals) or (
            len(_name_parts(crm.client_name)) >= 2 and set(_name_parts(crm.client_name)) <= tokens(cal.title)
        )
        add("person", 1.0 if hit else 0.0, f"client {crm.client_name} is an attendee / named in the title")

    if crm.client_company:
        hit = company_hit(crm.client_company, cal)
        if hit is not None:
            add("company", 1.0 if hit else 0.0, f"company '{crm.client_company}' matches attendee domain or title")

    tj = jaccard(tokens(crm.title), tokens(cal.title))
    add("title", tj, f"title similarity {tj:.2f}")

    if crm.location and cal.location:
        add("location", 1.0 if locations_compatible(crm.location, cal.location) else 0.0, "locations compatible")

    if crm.is_internal is not None and cal.is_internal is not None and (crm.is_internal or cal.is_internal):
        add("internal", 1.0 if (crm.is_internal and cal.is_internal) else 0.0,
            "both are internal-only meetings")

    if avail < 0.4:                      # not enough comparable evidence to trust a score
        return 0.0, []
    return round(got / avail, 3), reasons


@dataclass
class Match:
    crm: Record
    cal: Record
    score: float
    reasons: list


def match_sources(crm: list, cal: list, threshold: float = MATCH_THRESHOLD) -> tuple[list, list, list]:
    """One-to-one assignment, best score first. Returns (matches, unmatched_crm, unmatched_cal)."""
    pairs = []
    for c in crm:
        for k in cal:
            s, reasons = score_pair(c, k)
            if s >= threshold:
                pairs.append((s, c, k, reasons))
    pairs.sort(key=lambda p: (-p[0], p[1].id, p[2].id))
    used_crm, used_cal, matches = set(), set(), []
    for s, c, k, reasons in pairs:
        if c.id in used_crm or k.id in used_cal:
            continue
        used_crm.add(c.id)
        used_cal.add(k.id)
        matches.append(Match(c, k, s, reasons))
    return (
        matches,
        [c for c in crm if c.id not in used_crm],
        [k for k in cal if k.id not in used_cal],
    )


def closest_candidate(rec: Record, others: list) -> Optional[dict]:
    """For an unmatched record: why did the best same-day candidate not qualify?"""
    same_day = [o for o in others if o.date and o.date == rec.date]
    if not same_day:
        return {"id": None, "score": 0.0, "note": "no records from the other system on this date"}
    best = None
    for o in same_day:
        s, _ = (score_pair(rec, o) if rec.source == "crm" else score_pair(o, rec))
        if best is None or s > best[0]:
            best = (s, o)
    return {
        "id": best[1].id,
        "title": best[1].title,
        "score": best[0],
        "note": f"closest same-day record scored {best[0]:.2f}, below the {MATCH_THRESHOLD:.2f} threshold",
    }


# --------------------------------------------------------------------------- within-source duplicates
def _dup_reasons(a: Record, b: Record) -> Optional[list]:
    if a.date is None or a.date != b.date or a.start is None or b.start is None:
        return None
    gap = abs((a.start - b.start).total_seconds()) / 60
    overlap = bool(a.end and b.end and a.start < b.end and b.start < a.end)
    if gap > 60 and not overlap:
        return None
    reasons = [f"same date, starts {gap:.0f} min apart" + (" and the time ranges overlap" if overlap else "")]
    strong = False
    shared = sorted(set(a.externals) & set(b.externals))
    if shared:
        reasons.append("shared external attendee(s): " + ", ".join(shared))
        strong = True
    if a.client_name and a.client_name == b.client_name and a.client_name.lower() not in PLACEHOLDER_NAMES:
        reasons.append(f"same client ({a.client_name})")
        strong = True
    tj = jaccard(tokens(a.title), tokens(b.title))
    if tj >= 0.6:
        reasons.append(f"near-identical titles ({tj:.2f})")
        strong = True
    if a.organizer and a.organizer == b.organizer:
        reasons.append("same organizer")
    return reasons if strong else None


def dedupe(records: list) -> list:
    """Fold same-source duplicates into one primary record each; return the primaries.

    Only *same-day, near-in-time* records that also share an external person (or client, or title)
    are folded. Identical titles on different days (a recurring "Weekly Team Sync") are separate
    occurrences and are deliberately left alone.
    """
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    reasons_for: dict = {}
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            r = _dup_reasons(records[i], records[j])
            if r:
                parent[find(j)] = find(i)
                reasons_for[(i, j)] = r

    clusters: dict = {}
    for i in range(len(records)):
        clusters.setdefault(find(i), []).append(i)

    out = []
    for members in clusters.values():
        group = [records[i] for i in members]
        if len(group) == 1:
            out.append(group[0])
            continue
        # most recently created entry wins; file order breaks ties
        primary = max(group, key=lambda r: (r.created_at.timestamp() if r.created_at else 0, records.index(r)))
        for other in group:
            if other is primary:
                continue
            primary.merged_ids.append(other.id)
            primary.merged_raw.append(other.raw)
            primary.alternates.append({
                "id": other.id,
                "title": other.title,
                "start": other.start.strftime("%Y-%m-%d %H:%M") if other.start else None,
                "end": other.end.strftime("%Y-%m-%d %H:%M") if other.end else None,
                "location": other.location,
                "created_at": other.created_at.isoformat() if other.created_at else None,
            })
            primary.warnings.extend(other.warnings)
            for a in other.attendees:
                if a not in primary.attendees:
                    primary.attendees.append(a)
            primary.externals = [e for e in primary.attendees if not e.endswith("@" + INTERNAL_DOMAIN)]
            primary.placeholder_attendees += [p for p in other.placeholder_attendees
                                              if p not in primary.placeholder_attendees]
        idx = {records[i].id: i for i in members}
        for (i, j), r in reasons_for.items():
            if records[i].id in idx and records[j].id in idx:
                primary.dup_reasons += r
        primary.dup_reasons = list(dict.fromkeys(primary.dup_reasons))
        merged = " and ".join([primary.id] + primary.merged_ids)
        primary.warnings.append({
            "source": primary.source, "record_id": primary.id, "field": "(record)", "kind": "duplicate",
            "message": f"{merged} appear to be the same meeting ({'; '.join(primary.dup_reasons)}). "
                       f"Merged into one; {primary.id} kept as primary because it was created most recently.",
        })
        out.append(primary)
    return out
