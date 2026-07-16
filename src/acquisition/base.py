"""
Layer 1 — Acquisition.

`DataSource` is the seam that keeps the rest of the platform independent of
where data comes from. Two implementations exist today:

  * `SyntheticDataSource`  — the MAP-line simulator (development / demo)
  * `AlarmLogDataSource`   — a real historian alarm-transaction export (CSV)

`PIHistorianDataSource` / `OpcUaDataSource` (live feeds) implement the SAME two
methods, so nothing downstream changes when they land.

Note on `value`: continuous-process sources attach the tripping value to each
event. A DCS/BMS *alarm log* usually carries no analogue value at all — only the
transition — so `value` is optional and `description` (the operator-facing alarm
text) carries the meaning instead.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ProcessSample:
    ts: datetime
    tag: str
    value: float


@dataclass
class AlarmEvent:
    ts: datetime
    tag: str            # the alarm source (instrument tag / asset id)
    level: str          # LO/LOLO/HI/HIHI (limit alarms) or an alarm-type code
    priority: str       # LOW / MEDIUM / HIGH / CRITICAL
    state: str          # ACTIVE / CLEARED
    value: float | None = None
    incident_id: str | None = None   # ground-truth link (None in real data)
    description: str | None = None   # operator-facing alarm text
    zone: str | None = None          # console / area owning the alarm


class DataSource(ABC):
    """Any data source yields two streams: continuous samples and discrete
    alarm events, both already timestamped."""

    @abstractmethod
    def process_samples(self) -> list[ProcessSample]:
        ...

    @abstractmethod
    def alarm_events(self) -> list[AlarmEvent]:
        ...
