# AI collaboration

The brief says AI assistance is encouraged and asks for transparency, so this folder records how it was used.

> **Author's note (edit before submitting):** _The notes below were written by the AI assistant during the
> session. Add your own account here — what you directed, what you reviewed and changed, what you decided
> differently, and your honest time breakdown._

## How AI was used

| Area | Role of the AI | Where a human should check |
|---|---|---|
| Data analysis | Read every record in both files, listed expected matches, conflicts and malformed fields ([01](01-data-analysis.md)) | Confirm the expected-matches table against your own reading of the data |
| Design | Proposed the pipeline and the trade-offs ([02](02-design-decisions.md)) | The *judgement calls* — conflict policy, primary-of-duplicates, timezone assumption |
| Implementation | Wrote the backend, frontend and tests | Read the code; the matching weights in `app/matching.py` are the part most worth understanding |
| Verification | Ran the tests, started the real app via `python run.py`, loaded it in headless Chrome, reviewed screenshots | Click through the UI yourself |
| Documentation | Drafted the README and these notes | Fill in time spent; make sure the README reflects *your* understanding |

## What was verified, and how

* 22 automated tests pass (`python -m pytest`), one per tricky case in the brief plus API and robustness checks.
* The single command `python run.py` was run from a clean state (no `.venv`) and served the app.
* The UI was loaded in headless Chrome and screenshots were inspected for the list view and two detail views.
* The matching threshold was checked against the data: weakest accepted pair 0.734, strongest rejected 0.286.

## What was *not* verified

* No labelled ground truth exists, so "correct" matches are my reading of the data, not an external answer key.
* The frontend has no automated tests.
* Only Python 3.13 on Linux was exercised.

## Index

1. [`01-data-analysis.md`](01-data-analysis.md)
2. [`02-design-decisions.md`](02-design-decisions.md)
3. [`03-process-log.md`](03-process-log.md)
