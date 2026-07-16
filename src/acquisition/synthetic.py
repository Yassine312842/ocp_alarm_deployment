"""
Layer 1 — Synthetic data source.

Simulates a MAP line producing:
  * continuous process samples (normal operation + drift + noise),
  * threshold alarms when a tag crosses its LO/LOLO/HI/HIHI limit,
  * recurring INCIDENTS with a known root cause and a cascading alarm chain,
  * one CHATTERING bad-actor instrument.

The point: generate data with *discoverable structure* so Layers 3-4 have
real patterns (Pareto bad actors, floods, co-occurrence, pre-incident
sequences, and known root causes to validate against).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from config import (CHATTERING_TAG, PRIORITY_BY_LEVEL, SCENARIOS, TAGS,
                    TAGS_BY_NAME, Tag)
from .base import AlarmEvent, DataSource, ProcessSample

_LEVEL_ATTR = {"LO": "lo", "LOLO": "lolo", "HI": "hi", "HIHI": "hihi"}


def _limit(tag: Tag, level: str) -> float | None:
    return getattr(tag, _LEVEL_ATTR[level])


class SyntheticDataSource(DataSource):
    def __init__(self, *, days: int = 14, sample_seconds: int = 60,
                 incidents_per_day: float = 1.4, seed: int = 7):
        self.days = days
        self.sample_seconds = sample_seconds
        self.incidents_per_day = incidents_per_day
        self.rng = random.Random(seed)
        self.start = datetime(2025, 1, 1, 0, 0, 0)
        self._samples: list[ProcessSample] = []
        self._alarms: list[AlarmEvent] = []
        self._generate()

    # ------------------------------------------------------------------ #
    def _generate(self) -> None:
        self._plan_incidents()
        self._simulate()
        self._chatter()
        self._samples.sort(key=lambda s: s.ts)
        self._alarms.sort(key=lambda a: a.ts)

    def _plan_incidents(self) -> None:
        """Pick incident start times and scenarios up front."""
        self.incidents: list[dict] = []
        n = int(self.days * self.incidents_per_day)
        horizon = self.days * 24 * 60  # minutes
        for i in range(n):
            scen = self.rng.choice(SCENARIOS)
            t0 = self.start + timedelta(minutes=self.rng.randint(30, horizon - 60))
            self.incidents.append({
                "id": f"INC-{i:03d}",
                "scenario": scen.key,
                "root_cause": scen.root_cause,
                "chain": scen.chain,
                "t0": t0,
            })
        self.incidents.sort(key=lambda d: d["t0"])

    def _emit_alarm(self, ts, tag_name, level, value, incident_id=None):
        self._alarms.append(AlarmEvent(
            ts=ts, tag=tag_name, level=level,
            priority=PRIORITY_BY_LEVEL[level], state="ACTIVE",
            value=round(value, 3), incident_id=incident_id))
        # a matching CLEARED event a few minutes later (bounded alarm)
        self._alarms.append(AlarmEvent(
            ts=ts + timedelta(minutes=self.rng.randint(3, 12)),
            tag=tag_name, level=level, priority=PRIORITY_BY_LEVEL[level],
            state="CLEARED", value=round(value, 3), incident_id=incident_id))

    def _simulate(self) -> None:
        """Continuous sampling; fire chain alarms when an incident is active."""
        steps = self.days * 24 * 60 * 60 // self.sample_seconds
        for k in range(steps):
            ts = self.start + timedelta(seconds=k * self.sample_seconds)
            for tag in TAGS:
                drift = 0.4 * tag.noise * self._slow_wave(ts, tag.name)
                value = tag.setpoint + drift + self.rng.gauss(0, tag.noise)
                self._samples.append(ProcessSample(ts, tag.name, round(value, 3)))

        # incident chains: emit ordered alarms at t0 + delay
        for inc in self.incidents:
            for (tag_name, level, delay_min) in inc["chain"]:
                tag = TAGS_BY_NAME[tag_name]
                lim = _limit(tag, level)
                if lim is None:
                    continue
                # value just past the limit, on the correct side
                sign = 1 if level in ("HI", "HIHI") else -1
                value = lim + sign * abs(tag.noise) * self.rng.uniform(1.0, 2.5)
                self._emit_alarm(inc["t0"] + timedelta(minutes=delay_min),
                                 tag_name, level, value, inc["id"])

    def _chatter(self) -> None:
        """A faulty transmitter toggling its LO alarm many times a day."""
        tag = TAGS_BY_NAME[CHATTERING_TAG]
        for day in range(self.days):
            base = self.start + timedelta(days=day, hours=self.rng.randint(2, 20))
            for j in range(self.rng.randint(20, 45)):  # burst of nuisance alarms
                ts = base + timedelta(seconds=j * self.rng.randint(20, 90))
                val = (tag.lo or tag.setpoint) - abs(tag.noise)
                self._alarms.append(AlarmEvent(
                    ts=ts, tag=tag.name, level="LO", priority="LOW",
                    state="ACTIVE", value=round(val, 3)))
                self._alarms.append(AlarmEvent(
                    ts=ts + timedelta(seconds=self.rng.randint(5, 25)),
                    tag=tag.name, level="LO", priority="LOW",
                    state="CLEARED", value=round(val, 3)))

    def _slow_wave(self, ts: datetime, tag_name: str) -> float:
        import math
        phase = hash(tag_name) % 100
        minutes = (ts - self.start).total_seconds() / 60.0
        return math.sin((minutes + phase) / 180.0)

    # ------------------------------------------------------------------ #
    def process_samples(self) -> list[ProcessSample]:
        return self._samples

    def alarm_events(self) -> list[AlarmEvent]:
        return self._alarms

    def incident_ground_truth(self) -> list[dict]:
        """Only available for synthetic data — used to validate root-cause output."""
        return [{"incident_id": i["id"], "scenario": i["scenario"],
                 "root_cause": i["root_cause"], "t0": i["t0"]}
                for i in self.incidents]
