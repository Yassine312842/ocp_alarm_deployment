"""
Layer 1 — Real data source: a historian alarm-transaction export (CSV).

This is the connector the whole skeleton was built to accept. It implements the
same `DataSource` interface as `SyntheticDataSource`, so Layers 2-6 are
untouched.

The export it reads (`preprocessed_trendedpointalarm.csv`) is an IBMS alarm log:

    DateTime, ProcessID, AssetID, AlarmSeverityName, State, TransactionMessage,
    Stage, AlarmClassName, Year, Month, Day, DayOfWeek, Season, Hour,
    ProcessedMessage

Three things have to be decided to map it onto the platform's `AlarmEvent`, and
each one is a judgement call worth stating out loud:

1. **tag** = `AssetID`. The asset is the alarm *source* — the thing you would
   rationalise, repair, or re-tune. Assets with no id (271 rows) become
   `UNKNOWN-ASSET` rather than being dropped: they are still operator load.

2. **level** = an alarm-type code derived from `ProcessedMessage`. A BMS has no
   LO/LOLO/HI/HIHI limit ladder, but the analytics only ever use `level` as
   "which *kind* of alarm on this tag" — which is exactly what the message is.
   85 free-text messages collapse to ~16 stable codes (see `ALARM_TYPE_RULES`),
   which is what makes chattering and sequence mining meaningful.

3. **priority** = `AlarmSeverityName` mapped to the ISA-18.2 4-tier scale, with
   safety-related classes (`LifeSafety`, gas, trip) promoted to CRITICAL
   regardless of the configured severity. The site's own priority assignment is
   trusted otherwise — including where it is obviously inflated. That inflation
   is a *finding*, not something to quietly correct in the loader.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.preparation.events import reconstruct_episodes
from .base import AlarmEvent, DataSource, ProcessSample

# --- 1. alarm-type taxonomy ------------------------------------------------
# Ordered: first match wins. Keep the safety types above the generic ones.
ALARM_TYPE_RULES: list[tuple[str, str]] = [
    (r"gas (leak|emergency)|gasm",            "GAS"),
    (r"water leak",                           "WATER_LEAK"),
    (r"trip alarm",                           "TRIP"),
    (r"trouble|supervisory|alarm signal|fas", "FIRE_SUPERVISORY"),
    (r"door open|gate barrier",               "SECURITY"),
    (r"offline",                              "OFFLINE"),
    (r"bacnet error|error|fully operative|device operations", "COMMS_FAULT"),
    (r"space temp",                           "SPACE_TEMP"),
    (r"set ?point",                           "SETPOINT"),
    (r"(high|hex high).*temp|temp.*high|high temp", "TEMP_HIGH"),
    (r"low.*temp|temp.*low",                  "TEMP_LOW"),
    (r"(dis|ra|ma|return) (air )?temp",       "AIR_TEMP"),
    (r"\bfan\b",                              "FAN"),
    (r"chiller",                              "CHILLER"),
    (r"manual mode",                          "MANUAL_MODE"),
    (r"consumption|meter",                    "METER"),
    (r"battery",                              "POWER"),
]
_COMPILED = [(re.compile(p), code) for p, code in ALARM_TYPE_RULES]

# What each alarm type usually means — seeds the knowledge base / RCA hints.
TYPE_CAUSE_HINT: dict[str, str] = {
    "OFFLINE":          "Controller or network segment lost — check gateway, power and BACnet trunk",
    "COMMS_FAULT":      "Field-bus / device communication error",
    "SPACE_TEMP":       "VAV cannot hold space setpoint — check damper, reheat and airflow",
    "SETPOINT":         "Setpoint deviation — schedule, occupancy or operator override",
    "TEMP_HIGH":        "Cooling shortfall — check chilled-water supply, valve and coil",
    "TEMP_LOW":         "Over-cooling / heating loss — check valve and controller output",
    "AIR_TEMP":         "AHU/MAU discharge temperature off target — coil, valve or sensor",
    "FAN":              "Fan command/status mismatch — starter, VFD or airflow proving",
    "CHILLER":          "Chiller start/stop transition",
    "GAS":              "Gas detection — safety response required",
    "WATER_LEAK":       "Leak detection — isolate and inspect",
    "FIRE_SUPERVISORY": "Fire-system supervisory / trouble condition",
    "SECURITY":         "Access-control event",
    "TRIP":             "Equipment trip",
    "MANUAL_MODE":      "Point left in manual / override — alarm suppressed at source",
    "METER":            "Metering fault or consumption threshold",
    "POWER":            "Power / battery system fault",
    "GENERAL":          "Uncategorised device alarm — review point configuration",
}

# --- 2. priority mapping (ISA-18.2 4-tier) ---------------------------------
SEVERITY_TO_PRIORITY = {
    "1 - high": "HIGH",
    "2 - medium": "MEDIUM",
    "3 - low": "LOW",
    "central battery system-medium": "MEDIUM",
}
SAFETY_CLASSES = {"lifesafety"}
SAFETY_TYPES = {"GAS", "TRIP", "FIRE_SUPERVISORY"}

PRIORITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def classify_alarm_type(message: str | None) -> str:
    if not message or not str(message).strip():
        return "GENERAL"
    m = str(message).lower()
    for rx, code in _COMPILED:
        if rx.search(m):
            return code
    return "GENERAL"


def map_priority(severity: str | None, alarm_class: str | None,
                 alarm_type: str) -> str:
    cls = (alarm_class or "").strip().lower()
    if cls in SAFETY_CLASSES or alarm_type in SAFETY_TYPES:
        return "CRITICAL"
    sev = (severity or "").strip().lower()
    if sev in SEVERITY_TO_PRIORITY:
        return SEVERITY_TO_PRIORITY[sev]
    if cls.endswith("-high"):
        return "HIGH"
    return "LOW"


def zone_of(asset_id: str | None) -> str:
    """Console / area that owns the alarm.

    Asset ids are structured `<building>-<block>-<block>-<floor>-<room>-...`
    (e.g. `1-JK1-JK1-00-D.01-AC-ACON-VAVU-0021`). Block + floor is the closest
    proxy this dataset has for an operator's span of control, which EEMUA-191
    rate metrics are defined per.
    """
    if not asset_id or not str(asset_id).strip():
        return "UNASSIGNED"
    parts = str(asset_id).split("-")
    if len(parts) >= 4:
        return f"{parts[1]}-{parts[3]}"
    return parts[0]


class AlarmLogDataSource(DataSource):
    """Reads a historian alarm-transaction CSV and emits ACTIVE/CLEARED events."""

    def __init__(self, csv_path: str | Path, *,
                 start: datetime | None = None,
                 end: datetime | None = None,
                 datetime_format: str = "%d-%m-%Y %H:%M"):
        self.csv_path = Path(csv_path)
        self.start, self.end = start, end
        self.datetime_format = datetime_format
        self._tx = self._load()
        self._events, self._episodes = reconstruct_episodes(self._tx)

    # ------------------------------------------------------------------ #
    def _load(self) -> pd.DataFrame:
        # utf-8 first; fall back to latin-1 (cp1252 superset) so a historian
        # export with accented text or stray bytes never fails to load.
        try:
            raw = pd.read_csv(self.csv_path, encoding="utf-8")
        except UnicodeDecodeError:
            raw = pd.read_csv(self.csv_path, encoding="latin-1")
        df = pd.DataFrame({
            "ts": pd.to_datetime(raw["DateTime"], format=self.datetime_format,
                                 errors="coerce"),
            "tag": raw["AssetID"].fillna("UNKNOWN-ASSET").astype(str).str.strip(),
            "transition": raw["State"].astype(str).str.strip().str.upper(),
            "message": raw["ProcessedMessage"].fillna(
                raw["TransactionMessage"]).fillna("").astype(str).str.strip(),
            "severity": raw["AlarmSeverityName"].astype(str),
            "alarm_class": raw["AlarmClassName"].astype(str),
            "stage": raw["Stage"].fillna("Unknown").astype(str),
        })

        self.rejected = int(df["ts"].isna().sum())
        df = df.dropna(subset=["ts"])
        if self.start is not None:
            df = df[df["ts"] >= self.start]
        if self.end is not None:
            df = df[df["ts"] <= self.end]

        df["level"] = df["message"].map(classify_alarm_type)
        df["priority"] = [map_priority(s, c, t) for s, c, t
                          in zip(df["severity"], df["alarm_class"], df["level"])]
        df["zone"] = df["tag"].map(zone_of)
        df["description"] = df["message"].str.slice(0, 120)
        # transitions we don't understand are dropped rather than guessed at
        known = df["transition"].isin(["N2A", "A2A", "A2N"])
        self.rejected += int((~known).sum())
        return df[known].reset_index(drop=True)

    # ------------------------------------------------------------------ #
    def process_samples(self) -> list[ProcessSample]:
        """An alarm log carries no continuous process values.

        Empty by design — the platform's analytics are event-based. When a
        historian feed (PI / OPC-UA) is added, that source fills this stream and
        Layer 2's `align_tags` starts doing work.
        """
        return []

    def alarm_events(self) -> list[AlarmEvent]:
        return [AlarmEvent(**r) for r in self._events.to_dict("records")]

    # -- extras beyond the interface, used by the KPI layer -------------- #
    def events_df(self) -> pd.DataFrame:
        return self._events

    def episodes_df(self) -> pd.DataFrame:
        return self._episodes

    def transactions_df(self) -> pd.DataFrame:
        return self._tx
