# 01 — Data analysis

Done before writing any code: read all 20 CRM and 22 calendar records and decided what the right answer
should be for each. The tests encode these expectations.

## Shape of the two sources

| | CRM (`crm_events.json`) | Calendar (`calendar_events.json`) |
|---|---|---|
| Records | 20 | 22 |
| ID | `crm_id` (`CRM-1001…`) | `event_id` (`CAL-A1…`) |
| When | `meeting_date` + `meeting_time` (separate, zoneless) | `start_time` / `end_time` (ISO; one is UTC `Z`) |
| Who | `client_name`, `client_company`, `relationship_owner` | `organizer`, `attendees[]` (emails) |
| Where | `location`, `meeting_type` (In-Person / Virtual / Internal) | `location` only (format must be inferred) |
| Other | `notes`, `status` (Completed/Confirmed/Scheduled/Tentative/Cancelled) | `description`, `status`, `is_recurring` |

Nothing links the two: no shared ID, no shared field name. Matching has to use the *content*: date, time,
person, company, title.

## Expected outcome per record

| CRM | Calendar | Expected treatment |
|---|---|---|
| CRM-1001 | CAL-A1 | Clean match. `HQ - Conference Room B` ⊃ `Conference Room B` (compatible). Completed vs confirmed (compatible) |
| CRM-1002 | CAL-A2 | **Conflict:** CRM In-Person at "NYC Office - 30th Floor" vs calendar Zoom |
| CRM-1003 | — | CRM only (Lakeshore intro call, no calendar entry). Location null |
| CRM-1004 | CAL-A4 | Match. Calendar time is `19:00Z`; 13 Mar 2025 is during US daylight time → 15:00 EDT vs CRM 14:00. **1 h conflict, probably an offset assumption** |
| CRM-1005 | CAL-A5 + CAL-A6 | **In-source duplicate** (one Pinnacle meeting, 11:00 and 11:30, same organiser, shared attendee). Merge, then match to the CRM |
| CRM-1006 | CAL-A7 | Match. Internal, recurring, no client |
| CRM-1007 | CAL-A8 | Match. CRM time and location are null → filled from calendar; Virtual ≈ Zoom |
| CRM-1008 | CAL-A9 | Match. **Malformed CRM date** `03-15/2025` → 2025-03-15 |
| CRM-1009 | CAL-A10 | Match. **Status conflict:** CRM Cancelled ("rescheduled to 3/26") vs calendar confirmed. No 3/26 board-prep entry exists |
| CRM-1010 | — | CRM only (Northwind, tentative, empty notes) |
| CRM-1011 | CAL-A12 | Match. "NYC Office" vs "NYC Office - 12th Floor" compatible. Company name differs from email domain (`horizonwp.com`) |
| CRM-1012 | CAL-A13 | Clean match |
| CRM-1013 | CAL-A14 | Match via abbreviation (`LPAC` = LP Advisory Committee). Organiser Priya ≠ owner Sarah (different roles) |
| CRM-1014 | CAL-A15 | Match via person name in attendee email. Location null on both sides |
| CRM-1015 | CAL-A16 | Match. **Malformed attendee** `raj.patel[at]atlasvc.com` |
| CRM-1016 | CAL-A17 | Match. **Conflict:** 13:00 vs 15:00 |
| CRM-1017 | CAL-A20 | Match. Placeholder client "Multiple", placeholder attendee `external-guests`, "12 attendees" vs 6 listed. "The Palm - DC" vs "The Palm Restaurant" compatible |
| CRM-1018 | CAL-A21 | Match. CRM location null; calendar says Zoom |
| CRM-1019 | CAL-A22 | Clean match (QBR = Quarterly Business Review) |
| CRM-1020 | — | CRM only (Northwind Fund VII, 2 Apr) |
| — | CAL-A3, CAL-A18 | Calendar only. **Same title on different days = two occurrences of a recurring meeting, not duplicates** |
| — | CAL-A11 | Calendar only. Sparse: no location, no description, no attendees, end time lacks seconds |
| — | CAL-A19 | Calendar only (Fund VII roadshow prep) |

**Totals:** 17 matched pairs (one using two calendar entries), 3 CRM-only, 4 calendar-only → **24 meetings**.

## Traps in the data worth naming

* **Look-alikes that must *not* merge:** same client on different days (Meridian 10 Mar vs 18 Mar; Crestview
  13 Mar vs 25 Mar; Atlas lunch vs deep-dive); the two Weekly Team Syncs.
* **Look-alikes that *must* merge:** `CAL-A5`/`CAL-A6`.
* **Not conflicts although they differ:** compatible locations, completed vs confirmed, owner vs organiser,
  differently-worded titles and notes.
* **A tempting but wrong match:** `CRM-1006` (internal, 16:00) vs `CAL-A11` (18:00, no attendees) share a date
  and nothing else; it scores 0.29 and is correctly rejected.
