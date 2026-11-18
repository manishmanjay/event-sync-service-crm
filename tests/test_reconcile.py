"""Behavioural tests: one per tricky case called out in the brief.

Run with:  pip install -r requirements-dev.txt && python -m pytest
"""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reconcile import reconcile_all

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def result():
    return reconcile_all(DATA)


def by_source_id(result, source_id):
    for m in result["meetings"]:
        if source_id in m["source_ids"]["crm"] + m["source_ids"]["calendar"]:
            return m
    raise AssertionError(f"{source_id} not found in any meeting")


def conflict(meeting, field):
    return next((c for c in meeting["conflicts"] if c["field"] == field), None)


def test_every_input_record_is_accounted_for_exactly_once(result):
    ids = [i for m in result["meetings"] for src in m["source_ids"].values() for i in src]
    assert len(ids) == len(set(ids)) == 20 + 22


def test_headline_counts(result):
    c = result["summary"]["counts"]
    assert (c["meetings"], c["both"], c["crm_only"], c["calendar_only"]) == (24, 17, 3, 4)
    assert c["duplicates_merged"] == 1 and c["rejected"] == 0


def test_no_shared_id_needed_records_still_join(result):
    m = by_source_id(result, "CRM-1001")
    assert "CAL-A1" in m["source_ids"]["calendar"] and m["presence"] == "both"


def test_in_source_duplicate_is_merged_and_joined_to_crm(result):
    m = by_source_id(result, "CRM-1005")
    assert set(m["source_ids"]["calendar"]) == {"CAL-A5", "CAL-A6"}
    assert m["duplicates"][0]["primary"] == "CAL-A6"      # most recently created
    attendees = next(f for f in m["fields"] if f["name"] == "attendees")["resolved"]
    assert "sandra.mills@pinnaclegp.com" in attendees and "kevin.obrien@pinnaclegp.com" in attendees


def test_merged_duplicate_keeps_most_specific_location(result):
    m = by_source_id(result, "CRM-1005")
    loc = next(f for f in m["fields"] if f["name"] == "location")
    assert loc["resolved"] == "Boston Office - Room 301" and "CAL-A5" in loc["detail"]


def test_recurring_occurrences_are_not_deduplicated(result):
    syncs = [m for m in result["meetings"] if m["title"] == "Weekly Team Sync"]
    assert len(syncs) == 2 and {m["date"] for m in syncs} == {"2025-03-11", "2025-03-18"}


def test_same_client_different_day_stays_separate(result):
    assert by_source_id(result, "CRM-1001") is not by_source_id(result, "CRM-1007")
    assert by_source_id(result, "CRM-1004") is not by_source_id(result, "CRM-1018")


def test_location_vs_format_conflict_zoom_vs_in_person(result):
    m = by_source_id(result, "CRM-1002")
    assert conflict(m, "format")["severity"] == "high"
    assert conflict(m, "location") is not None


def test_more_specific_location_is_compatible_not_conflict(result):
    m = by_source_id(result, "CRM-1011")             # "NYC Office" vs "NYC Office - 12th Floor"
    loc = next(f for f in m["fields"] if f["name"] == "location")
    assert loc["state"] == "compatible" and loc["resolved"] == "NYC Office - 12th Floor"
    assert conflict(m, "location") is None


def test_utc_timestamp_is_converted_with_dst_and_flagged(result):
    m = by_source_id(result, "CRM-1004")
    assert m["start_local"] == "2025-03-13 15:00"     # 19:00Z is EDT (UTC-4) on 13 March 2025
    c = conflict(m, "start")
    assert c and "offset assumption" in c["explanation"] and c["severity"] == "medium"


def test_two_hour_time_conflict_is_high_severity(result):
    c = conflict(by_source_id(result, "CRM-1016"), "start")
    assert c["severity"] == "high" and "120 min" in c["explanation"]


def test_cancelled_in_crm_but_confirmed_in_calendar(result):
    m = by_source_id(result, "CRM-1009")
    assert m["status"] == "cancelled" and conflict(m, "status")["severity"] == "high"


def test_malformed_crm_date_is_repaired_and_still_matches(result):
    m = by_source_id(result, "CRM-1008")
    assert m["date"] == "2025-03-15" and "CAL-A9" in m["source_ids"]["calendar"]
    assert any(w["kind"] == "repaired" and w["field"] == "meeting_date" for w in m["warnings"])


def test_missing_crm_time_is_filled_from_calendar_without_conflict(result):
    m = by_source_id(result, "CRM-1007")
    assert m["start_local"] == "2025-03-18 15:00" and conflict(m, "start") is None


def test_malformed_attendee_email_is_repaired(result):
    m = by_source_id(result, "CAL-A16")
    assert "raj.patel@atlasvc.com" in next(f for f in m["fields"] if f["name"] == "attendees")["resolved"]
    assert any(w["kind"] == "repaired" for w in m["warnings"])


def test_placeholder_attendee_is_flagged_and_count_mismatch_reported(result):
    m = by_source_id(result, "CRM-1017")
    assert any(w["kind"] == "placeholder" for w in m["warnings"])
    assert conflict(m, "attendees")["severity"] == "low"


def test_unmatched_records_explain_themselves(result):
    m = by_source_id(result, "CRM-1003")
    assert m["presence"] == "crm_only" and m["match"]["closest_candidate"]["note"]


def test_internal_meeting_title_abbreviation_matches(result):
    assert "CAL-A14" in by_source_id(result, "CRM-1013")["source_ids"]["calendar"]   # LPAC prep


def test_every_field_row_names_its_source(result):
    for m in result["meetings"]:
        for f in m["fields"]:
            if f["resolved"] is not None:
                assert f["resolved_from"] in {"crm", "calendar", "both"}, (m["id"], f["name"])


# --------------------------------------------------------------------------- robustness
def test_bad_records_are_rejected_not_fatal(tmp_path):
    crm = json.loads((DATA / "crm_events.json").read_text())
    cal = json.loads((DATA / "calendar_events.json").read_text())
    crm.append({"subject": "no id here"})
    cal.append("not an object")
    (tmp_path / "crm_events.json").write_text(json.dumps(crm))
    (tmp_path / "calendar_events.json").write_text(json.dumps(cal))
    r = reconcile_all(tmp_path)
    assert r["summary"]["counts"]["rejected"] == 2 and r["summary"]["counts"]["meetings"] == 24


def test_missing_data_file_gives_clear_error(tmp_path):
    shutil.copy(DATA / "crm_events.json", tmp_path / "crm_events.json")
    with pytest.raises(RuntimeError, match="calendar_events.json"):
        reconcile_all(tmp_path)


# --------------------------------------------------------------------------- API
def test_api_endpoints():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        listing = client.get("/api/meetings").json()
        assert listing["count"] == 24
        assert client.get("/api/meetings", params={"presence": "crm_only"}).json()["count"] == 3
        assert client.get("/api/meetings", params={"has_conflicts": "true"}).json()["count"] == 6
        assert client.get("/api/meetings", params={"q": "pinnacle"}).json()["count"] == 1
        mid = listing["meetings"][0]["id"]
        detail = client.get(f"/api/meetings/{mid}").json()
        assert detail["fields"] and "raw" in detail
        assert client.get("/api/meetings/nope").status_code == 404
        assert client.get("/api/meetings", params={"presence": "bogus"}).status_code == 422
        assert client.get("/").status_code == 200
        assert client.post("/api/reload").json()["meetings"] == 24
