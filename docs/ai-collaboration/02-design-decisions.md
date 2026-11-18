# 02 — Design decisions

Each entry: the question, the options, what was chosen, and when to revisit it.

## D1. Show disagreement instead of resolving it away
* **Options:** (a) merge into one record, last-write-wins; (b) merge but keep both values and flag conflicts.
* **Chosen:** (b). The brief explicitly wants users to *see* conflicts and which source each value came from.
  The resolved value is a best guess; the conflict list tells a human where to look.
* **Revisit if:** users want a hard single answer with no review step.

## D2. How to match records that share no ID
* **Options:** (a) exact match on date+time; (b) fuzzy-string library; (c) hand-weighted evidence score.
* **Chosen:** (c). (a) fails the moment times differ (`CRM-1016`, `CRM-1004`). (b) adds a dependency and gives
  a number nobody can explain. (c) is small, has no dependencies, and returns *reasons* with every match.
* **Detail:** signals that can't be evaluated are removed from the denominator rather than scored as zero.
* **Revisit if:** the dataset grows enough to evaluate against labelled pairs; then learn the weights.

## D3. Same local date is a hard requirement
* **Chosen:** different dates never match.
* **Why:** it prevents the most dangerous false positives (same client, different meeting — Meridian, Crestview,
  Atlas all appear twice). The cost is that a meeting moved to another day looks like two unmatched records.
* **Revisit if:** reschedules are common; add a "possible reschedule" hint rather than relaxing the gate.

## D4. De-duplicate within a source *before* matching across sources
* **Why:** otherwise `CAL-A5` and `CAL-A6` both score well against `CRM-1005`; one wins and the other is
  reported as an unrelated calendar-only meeting.
* **Rule:** same date, starts ≤ 60 min apart (or ranges overlap) **and** a shared external attendee / same
  client / near-identical title. "Same title" alone is not enough — that would merge the weekly syncs, which are
  on different days anyway.

## D5. Which source wins a field
* **Chosen:** calendar for scheduling facts (time, place, attendees); CRM for relationship facts (client,
  company, owner, notes). Status: `cancelled` wins.
* **Why:** a calendar entry is what people were actually invited to; the CRM is where relationships are recorded.
  Cancelled wins because someone explicitly cancelled it; a stale "confirmed" is more likely than a stale
  cancellation.
* **Always:** both raw values remain visible, and a contradiction is a recorded conflict.
* **Revisit if:** the business names a different system of record.

## D6. Timezone
* **Facts:** one calendar timestamp is UTC (`CAL-A4`, `19:00Z`); everything else is zoneless.
* **Chosen:** assume zoneless = `America/New_York`; convert UTC with real timezone rules. 19:00Z on 13 Mar 2025
  is 15:00 EDT (US daylight time began 9 Mar), so the CRM's 14:00 differs by an hour.
* **Why not just subtract 5 hours:** that would make the record match by accident and hide a real DST effect.
  Instead it is reported as a medium-severity conflict whose explanation says it is probably an offset
  assumption, not a reschedule.
* **Revisit if:** the sources can provide an offset or zone name.

## D7. Repair, warn, or reject
* **Repair + warn** when intent is unambiguous: `03-15/2025` → 2025-03-15; `raj.patel[at]…` → `@`; end time
  without seconds.
* **Keep empty + warn** otherwise: null time, null location, placeholder `Multiple`.
* **Reject + report** only when a record is structurally unusable (no ID, not an object). One bad record never
  stops ingestion.

## D8. Which parts of free text to trust
* **Chosen:** treat notes and descriptions as display-only, with one narrow exception: an "N attendees" count in
  CRM notes is compared with the invite list (low-severity conflict).
* **Why:** `CRM-1009`'s notes say "rescheduled to 3/26" but nothing exists on 3/26. Acting on free text would
  invent a meeting.

## D9. Primary record of a merged duplicate
* **Chosen:** the most recently created entry (`CAL-A6`); the other's disagreeing values are shown in the
  detail view, and the merged record takes the union of attendees and the most specific location.
* **Honest note:** this is a judgement call. `CAL-A5` happens to agree with the CRM on the time; `CAL-A6` is newer
  and has the extra attendee. Because the choice is surfaced as a conflict, it's easy to see and to flip.

## D10. Architecture
* **Chosen:** reconcile once at startup into memory; FastAPI serves JSON and a static page; plain JavaScript,
  no build step; `python run.py` bootstraps a virtualenv.
* **Why:** two small static files don't justify a database; a reviewer should be able to run it with one command
  and read every file.
* **Revisit if:** the sources become live APIs — then add scheduled ingestion and persistence.
