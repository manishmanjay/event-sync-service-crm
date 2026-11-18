"""Runtime configuration. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent

# Where crm_events.json and calendar_events.json live.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))

# Timezone used to (a) interpret zoneless timestamps and (b) display every time.
# All three offices in the data (DC, Boston, NYC) are on US Eastern time.
LOCAL_TZ_NAME = os.environ.get("LOCAL_TZ", "America/New_York")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

# Attendees on this domain are "us"; anyone else is an external contact.
INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN", "firma.com").lower()

# Minimum normalised match score (0..1) for a CRM record and a calendar record
# to be treated as the same meeting.
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.60"))
