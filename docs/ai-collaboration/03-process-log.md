# 03 — Process log

A chronological record of how the work actually went, including the stumbles.

1. **Brief received.** The brief refers to `/data/crm_events.json` and `/data/calendar_events.json`. Neither
   existed in the workspace and nothing was attached, so the assistant stopped and asked for the files rather
   than inventing sample data. Building the reconciler against made-up records would have meant guessing the
   matching rules and writing a README about decisions that were never actually made.
2. **Data attached.** Read both files in full and produced the record-by-record analysis in
   [`01-data-analysis.md`](01-data-analysis.md): expected matches, the one in-source duplicate, the conflicts,
   and the malformed fields.
3. **Design.** Settled the pipeline (normalise → de-duplicate → match → resolve) and the judgement calls in
   [`02-design-decisions.md`](02-design-decisions.md).
4. **Engine first, UI second.** Built `normalize.py`, `matching.py`, `reconcile.py`, then ran them on the real
   data and printed all 24 meetings with their scores and conflicts *before* writing any web code. The output
   matched the hand analysis (17 / 3 / 4; 7 conflicts; 1 duplicate).
5. **API and UI.** FastAPI routes, then a no-build single-page frontend.
6. **Tests.** 21 tests written against the expectations from step 2; all passed first time. (That is mildly
   suspicious on its own, which is why step 8 matters: tests written after seeing the output can encode its
   mistakes.)
7. **End-to-end run.** Deleted `.venv`, ran `python run.py`, confirmed it created the environment, installed
   dependencies and served the app. Loaded the UI in headless Chrome and inspected screenshots.
8. **A bug found by looking, not by the tests.** In the Pinnacle detail view the merged meeting showed
   "Boston Office", but the duplicate `CAL-A5` had the more specific "Boston Office - Room 301" — merging
   was throwing that information away. Fixed so a merge keeps the most specific compatible location, added a
   test (now 22), and re-ran everything.
9. **Threshold check.** Measured the score margin instead of asserting one: weakest accepted 0.734, strongest
   rejected 0.286, and any threshold from ~0.3 to ~0.73 gives identical matches.
10. **Docs.** README and these notes.

## Things the human should double-check

* **Time spent** in the README is a placeholder.
* The **DST interpretation of `CAL-A4`**. If the author's intent was "19:00Z is 14:00 local", the conflict on
  Crestview is an artefact of correct timezone handling, which the explanation text already says. Decide whether
  you want to keep it as a conflict or special-case it.
* The **primary-of-duplicate** rule (D9) and the **"calendar wins scheduling facts"** rule (D5) are policy
  choices that a stakeholder might make differently.
* The **matching weights** are hand-set. Read `app/matching.py` and be able to explain them.
