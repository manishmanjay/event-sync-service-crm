"""HTTP layer: a thin REST API over the reconciled data, plus the static frontend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DATA_DIR
from .reconcile import reconcile_all

log = logging.getLogger("event-sync")
STATIC_DIR = Path(__file__).parent / "static"

# The data set is tiny and static, so reconciliation runs once at startup and the result
# is held in memory. POST /api/reload re-reads the files without a restart.
STATE: dict = {}

# Fields that make up the lightweight rows in the list endpoint (full detail: /api/meetings/{id}).
LIST_FIELDS = (
    "id", "title", "date", "start_local", "end_local", "time_known", "presence", "source_ids",
    "status", "client", "owner", "format", "location", "conflict_count", "max_severity",
    "conflict_fields", "warning_count",
)


def _load() -> None:
    result = reconcile_all(DATA_DIR)
    STATE["meetings"] = result["meetings"]
    STATE["by_id"] = {m["id"]: m for m in result["meetings"]}
    STATE["summary"] = result["summary"]
    c = result["summary"]["counts"]
    log.info("Reconciled %s CRM + %s calendar records into %s meetings (%s with conflicts)",
             c["crm_records"], c["calendar_records"], c["meetings"], c["with_conflicts"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()          # fail fast, with a clear message, if the data files are unusable
    yield


app = FastAPI(
    title="Event Sync Service",
    version="1.0.0",
    description="Reconciles CRM meetings and calendar events into one unified meeting list.",
    lifespan=lifespan,
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "meetings": len(STATE.get("meetings", []))}


@app.get("/api/summary")
def summary() -> dict:
    """Counts, conflict/warning breakdowns, rejected records and the assumptions in force."""
    return STATE["summary"]


@app.get("/api/meetings")
def list_meetings(
    presence: Optional[Literal["both", "crm_only", "calendar_only"]] = Query(None, description="Which sources have it"),
    has_conflicts: Optional[bool] = Query(None, description="Only meetings with (or without) cross-source conflicts"),
    has_warnings: Optional[bool] = Query(None, description="Only meetings with (or without) data-quality warnings"),
    status: Optional[str] = Query(None, description="Resolved status, e.g. cancelled"),
    q: Optional[str] = Query(None, description="Free-text search over title, client, owner, location and source ids"),
) -> dict:
    rows = STATE["meetings"]
    if presence:
        rows = [m for m in rows if m["presence"] == presence]
    if has_conflicts is not None:
        rows = [m for m in rows if (m["conflict_count"] > 0) == has_conflicts]
    if has_warnings is not None:
        rows = [m for m in rows if (m["warning_count"] > 0) == has_warnings]
    if status:
        rows = [m for m in rows if (m["status"] or "").lower() == status.lower()]
    if q:
        needle = q.lower()
        rows = [m for m in rows if needle in " ".join(
            str(x) for x in (m["title"], m["client"], m["owner"], m["location"],
                             *m["source_ids"]["crm"], *m["source_ids"]["calendar"]) if x).lower()]
    return {"count": len(rows), "meetings": [{k: m[k] for k in LIST_FIELDS} for m in rows]}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str) -> dict:
    """Everything about one meeting: per-field values from each source, conflicts, warnings, raw records."""
    meeting = STATE["by_id"].get(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id!r}")
    return meeting


@app.post("/api/reload")
def reload_data() -> dict:
    """Re-read the data files. On failure the previous (good) result stays in place."""
    try:
        _load()
    except Exception as exc:  # noqa: BLE001 - surface any ingestion failure to the caller
        raise HTTPException(status_code=422, detail=f"Reload failed, keeping previous data: {exc}") from exc
    return STATE["summary"]["counts"]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
