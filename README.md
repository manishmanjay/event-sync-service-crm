# Event Sync Service

Pulls meeting data from two upstream systems (a **CRM** and a **Calendar**), works out which records are the
same real-world meeting, and serves one unified list through a REST API and a small web UI. Every value in the
UI is labelled with the system it came from, and every place where the two systems disagree is surfaced rather
than silently resolved.

**Result on the supplied data:** 20 CRM records + 22 calendar records → **24 unified meetings**
(17 in both systems, 3 CRM-only, 4 calendar-only), **1** in-source duplicate merged, **7 conflicts** across
6 meetings, and **11 meetings** carrying data-quality notes.

> **Time spent:** _TODO — author to fill in honestly before submitting (e.g. analysis / implementation / UI /
> tests / docs)._
> **AI usage:** built with Claude as a pair-programmer; see [AI collaboration](#ai-collaboration).

---

## 1. Run it

**Requirements:** Python 3.9+ (developed and tested on 3.13). No Node, no database, no build step.

```bash
python run.py
```

Then open **http://127.0.0.1:8000** (interactive API docs at `/docs`).

That one command creates a local `.venv`, installs `requirements.txt` the first time (FastAPI, Uvicorn, tzdata),
and starts the server. It uses only the standard library itself, so it behaves the same on macOS, Linux and
Windows (`py run.py`). Stop it with `Ctrl+C`.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
</details>

**Run the tests** (22 tests: one per tricky case in the brief, plus API and robustness checks):

```bash
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest
```

**Configuration** (all optional environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address (used by `run.py`) |
| `DATA_DIR` | `./data` | Folder containing `crm_events.json` and `calendar_events.json` |
| `LOCAL_TZ` | `America/New_York` | Timezone used for zoneless timestamps and for display |
| `INTERNAL_DOMAIN` | `firma.com` | Attendees on this domain are "us"; others are external contacts |
| `MATCH_THRESHOLD` | `0.60` | Minimum score for two records to be treated as one meeting |

The data files are read at startup. Edit them and `POST /api/reload` (or restart) to re-ingest.

---

## 2. Using the UI

* **Summary cards** — how many meetings, how many in each source, how many have conflicts / duplicates / data issues.
* **Filters** — free-text search, *All / In both / CRM only / Calendar only*, *Conflicts only*, *Data issues only*.
* **Click a row** to expand it. The detail view shows, top to bottom:
  1. **How the records were matched** — the score and every signal that contributed (or, for single-source
     meetings, the closest candidate in the other system and why it fell short).
  2. **Duplicates merged**, if any.
  3. **Conflicts between sources**, coloured by severity, each with a plain-English explanation.
  4. **Field by field** — the CRM value, the Calendar value, the **resolved value with a source badge**
     (CRM / Calendar / Both), and a status (*match, compatible, differs, CONFLICT, CRM only, Calendar only, …*).
  5. **Data-quality notes** — every repair, placeholder or missing value on the underlying records.
  6. The **raw source records**, untouched.
* Rows are deep-linkable: `http://127.0.0.1:8000/#mtg-60f0901d` opens that meeting on load.

---

## 3. Approach

```
 crm_events.json ──► normalise ──► de-duplicate ─┐
                      (clean, warn)  (same source) ├─► match ─► resolve fields ─► unified meetings ─► API ─► UI
 calendar_events.json ► normalise ──► de-duplicate ─┘  (score)   (+ conflicts)
```

1. **Normalise** each record into one internal shape. Dates/times are parsed into a single timezone, statuses
   mapped to one vocabulary, attendee emails cleaned, location text turned into a format (virtual / in-person).
   Anything repaired or unusable produces a *warning* on the record; nothing is silently "fixed", and the raw
   record is always kept. A structurally invalid record (no ID, not an object) is rejected and reported in
   `/api/summary` instead of crashing the run.
2. **De-duplicate within each source first.** Two records in the *same* system on the same day, close in
   time, that share an external attendee (or client, or near-identical title) are folded into one. This
   happens *before* cross-source matching so that two copies of a meeting don't compete for a single CRM record.
3. **Match across sources** with a transparent weighted score (details below), assigned one-to-one, best
   score first.
4. **Resolve every field** with an explicit per-field policy, keeping *both* source values and recording a
   conflict whenever both sides have a value and they contradict each other.

### How each case in the brief was handled

| Case from the brief | What the data contained | What the service does |
|---|---|---|
| **No shared ID** | Different ID schemes (`CRM-1001` vs `CAL-A1`) | Weighted evidence score over date, time, client↔attendee, company↔email domain, title, location |
| **In one source only** | 3 CRM-only, 4 calendar-only | Shown with a *CRM only / Calendar only* badge, plus the closest candidate in the other system and its score |
| **Timestamps don't match** | `CAL-A4` is stored as UTC (`19:00Z`) while everything else is zoneless; `CRM-1016` is 2 h off the calendar; `CRM-1005` is 30 min off | UTC is converted using real timezone rules (incl. DST). Remaining differences become **time conflicts** whose severity depends on the size of the gap |
| **Field conflicts** | Zoom vs In-Person (`CRM-1002`); Cancelled vs confirmed (`CRM-1009`); attendee count 12 vs 6 (`CRM-1017`) | Listed under *Conflicts between sources* with severity and explanation; the resolved value is shown with its source |
| **Duplicate within a source** | `CAL-A5` and `CAL-A6` are one Pinnacle meeting (same organiser, overlapping, shared attendee) | Merged into one calendar record (union of attendees, most specific location); primary is the more recently created. The disagreement between the two copies is shown, not hidden |
| **Missing / malformed fields** | CRM date `03-15/2025`; CRM time `null`; calendar end `…T20:00`; attendee `raj.patel[at]atlasvc.com`; attendee `external-guests`; client `"Multiple"`; null/empty locations & notes | Repaired where the intent is unambiguous, otherwise left empty — each one logged as a data-quality note on the meeting |

**Things deliberately *not* treated as problems:** the two `Weekly Team Sync` entries are two occurrences of a
recurring series on different days, not duplicates; `NYC Office` vs `NYC Office - 12th Floor` is one source being
more specific, not a conflict; "Completed" vs "confirmed" don't contradict; a calendar *organizer* differing from
the CRM *relationship owner* is a difference in roles, not a conflict.

### Matching in detail

Each candidate pair must fall on the **same local date**, then is scored from these signals:

| Signal | Weight | Counts as agreement when… |
|---|---|---|
| Start time | 0.35 | 0 min apart → 1.0, ≤15 → 0.85, ≤60 → 0.70, ≤120 → 0.45, ≤240 → 0.15 |
| Client person | 0.30 | CRM client's first+last name appears in an external attendee's email, or in the calendar title |
| Client company | 0.20 | A distinctive word from the company name (not "Capital", "Partners"…) appears in an attendee domain or the title |
| Title | 0.20 | Token overlap (Jaccard), with a few abbreviations expanded (`LPAC`, `QBR`, `DD`) |
| Location | 0.05 | One location is the same as, or a less specific form of, the other |
| Internal-only | 0.10 | Both sides describe an internal meeting |

Signals that **cannot be evaluated** for a pair (CRM has no time, internal meeting has no client, location is
null) are left out of the denominator rather than counted as a mismatch, so the score means *"how much of the
available evidence agrees"*. A pair needs enough comparable evidence to be scored at all, and must reach
**0.60**.

On this data the margin is wide: the **weakest accepted match scores 0.734** (`CRM-1016` ↔ `CAL-A17`, despite a
2-hour time gap) and the **strongest rejected pair scores 0.286**, so any threshold between ~0.3 and ~0.73 gives
identical results. Every match also reports *why* it was made, so a wrong match is easy to spot and debug.

### Field resolution policy

| Field | Preferred source | Rule |
|---|---|---|
| Start, end, location, attendees, recurrence | **Calendar** | It is what attendees were actually invited to. CRM is used only if the calendar has no value. When both are present and compatible, the more specific location wins |
| Client, company, owner, meeting type, notes | **CRM** | System of record for the relationship. Client is derived from external attendees only if the CRM has none |
| Title | Calendar | More descriptive; the CRM subject is still shown |
| Status | Either | `cancelled` beats everything (an explicit cancellation is more likely than a stale one); `completed` ≈ `confirmed`; otherwise the less certain value |
| Format (virtual / in-person) | Calendar | Inferred from the calendar location text (Zoom/Teams/virtual…) and compared with the CRM's meeting type |

The resolved value is a best guess; the **conflict** is how a human knows to double-check it.

### Conflicts found in the supplied data

| Meeting | Field | Severity | What differs |
|---|---|---|---|
| Summit Advisors (`CRM-1002`/`CAL-A2`) | Format | high | CRM In-Person vs calendar Zoom |
| Summit Advisors | Location | medium | "NYC Office - 30th Floor" vs Zoom link |
| Redwood ESG (`CRM-1016`/`CAL-A17`) | Start | high | 13:00 vs 15:00 |
| Board Prep (`CRM-1009`/`CAL-A10`) | Status | high | CRM Cancelled ("rescheduled to 3/26") vs calendar confirmed |
| Crestview DD (`CRM-1004`/`CAL-A4`) | Start | medium | CRM 14:00 vs calendar `19:00Z` = 15:00 EDT. Exactly one hour apart and the calendar side is UTC, so likely an offset assumption (agrees if entered as EST) rather than a reschedule — the explanation says so |
| Pinnacle (`CRM-1005`/`CAL-A5`+`A6`) | Start | medium | CRM 11:00 vs the newer calendar copy 11:30 (the older duplicate says 11:00) |
| Fund VII Closing Dinner (`CRM-1017`/`CAL-A20`) | Attendees | low | CRM notes say 12 attendees; calendar lists 6 entries, one a non-email placeholder |

---

## 4. Assumptions and key decisions

Assumptions I had to make (all visible in the UI footer and `/api/summary`):

1. **Zoneless timestamps are US Eastern.** All three offices in the data (DC, Boston, NYC) are Eastern, but the
   data never says so. Configurable with `LOCAL_TZ`.
2. **Two records only match if they fall on the same local date.** Safe failure mode: a meeting moved to another
   day shows up as two unmatched records rather than being wrongly merged.
3. **Ambiguous CRM dates are month-first** (US). In the data the one malformed date (`03-15/2025`) is
   unambiguous because 15 can't be a month.
4. **"Scheduled" and "Confirmed" mean the same thing** in the CRM.

Key decisions and the alternatives I rejected:

| Decision | Why | Rejected alternative |
|---|---|---|
| **Keep both values and flag conflicts** rather than overwrite | The point of the UI is to show disagreement; overwriting destroys the evidence | "Last write wins" merge |
| **Hand-weighted, explainable score** | 42 records can't train a model, and a reviewer needs to see *why* two records joined | Fuzzy-matching library / ML — opaque, extra dependency, nothing to train on |
| **De-duplicate within a source before matching** | Otherwise `CAL-A5`/`CAL-A6` fight for `CRM-1005` and one is mislabelled calendar-only | Match first, dedupe after |
| **Exclude non-evaluable signals from the score** | A missing CRM time shouldn't count as a mismatch | Treat missing as 0 (would fail `CRM-1007`) |
| **Warnings over silent fixes** | Each repair is a judgement the reader should be able to audit | Quietly normalise |
| **Compute once, hold in memory** | Data is two small static files; a database would add setup with no benefit | SQLite/Postgres |
| **FastAPI + plain JS, no build step** | One-command start; auto-generated `/docs`; nothing to compile | React/Vite SPA |
| **Don't parse free-text notes to change state** | `CRM-1009`'s note says "rescheduled to 3/26" but there is no such calendar entry; guessing would invent a meeting | NLP over notes |
| **Merged duplicate primary = most recently created** | Latest edit is most likely current; trivially switchable in `dedupe()` | Earliest / ask the user |

---

## 5. API

Interactive documentation: `/docs`.

| Method & path | Description |
|---|---|
| `GET /api/meetings` | Lightweight rows. Filters: `presence` (`both`/`crm_only`/`calendar_only`), `has_conflicts`, `has_warnings`, `status`, `q` (free text) |
| `GET /api/meetings/{id}` | Full detail: per-field CRM/Calendar/resolved values with source, conflicts, warnings, duplicates, match reasons, raw records |
| `GET /api/summary` | Counts, conflicts by field, warnings by kind, rejected records, assumptions in force |
| `POST /api/reload` | Re-read the data files (previous result is kept if the reload fails) |
| `GET /api/health` | Liveness |

Meeting IDs are deterministic (a hash of the source record IDs), so links stay stable across restarts.

```bash
curl 'http://127.0.0.1:8000/api/meetings?has_conflicts=true'
curl  http://127.0.0.1:8000/api/meetings/mtg-60f0901d
```

---

## 6. Project layout

```
run.py                 one-command launcher (venv + install + serve)
app/
  config.py            env-driven settings (timezone, threshold, data dir)
  models.py            internal Record shape
  normalize.py         load + clean each source, emit warnings
  matching.py          within-source dedupe, cross-source scoring and assignment
  reconcile.py         per-field resolution, conflicts, unified meeting objects
  main.py              FastAPI routes + static hosting
  static/              index.html, app.js, styles.css (no build)
data/                  crm_events.json, calendar_events.json
tests/                 22 behavioural tests
docs/ai-collaboration/ planning notes, data analysis, decision log, process log
```

---

## 7. Known limitations and what I'd do next

* **Weights are tuned by hand on 42 records** and checked by reading all 24 results; there is no labelled
  ground truth or held-out set. The score margin is wide here, but a larger real dataset would need a proper
  evaluation.
* **Same-day gate.** A meeting moved to a different day appears as two unmatched records. A "possible
  reschedule" hint (same people, nearby date) would be the natural next step — it would also help `CRM-1009`.
* **Free-text notes are display-only.** "CANCELLED — rescheduled to 3/26" is shown but not acted on.
* **Attendee-count check is a narrow heuristic** (looks for "N attendees" in CRM notes).
* **Recurring series are not expanded**; each calendar occurrence is its own meeting, which matches the data.
* **Timezone is an assumption**, not data. If the source systems can supply an offset or IANA zone, use it.
* **It's a batch reconcile, not live sync**: no polling, no persistence, no auth, no pagination (24 rows).
* **Frontend is not unit-tested**; it was checked by loading it in headless Chrome. Backend tested on
  Python 3.13 only (code avoids 3.10+ syntax so 3.9 should work).

---

## AI collaboration

This was built in a single working session with **Claude** (Anthropic) acting as a pair-programmer, as the brief
encourages. The planning notes and records of that collaboration are in
[`docs/ai-collaboration/`](docs/ai-collaboration/README.md):

* [`01-data-analysis.md`](docs/ai-collaboration/01-data-analysis.md) — record-by-record analysis of both files
* [`02-design-decisions.md`](docs/ai-collaboration/02-design-decisions.md) — options considered and why each choice was made
* [`03-process-log.md`](docs/ai-collaboration/03-process-log.md) — what was done in what order, what was verified, what was fixed along the way
