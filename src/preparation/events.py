"""
Layer 2 — Preparation for *event* data (as opposed to continuous samples).

A historian alarm log is a stream of state *transitions*, not a stream of alarm
occurrences. In this dataset (and in most BMS/DCS exports) the transition codes
are:

    N2A   normal -> alarm      the alarm was raised
    A2A   alarm  -> alarm      re-alarm / severity change while still in alarm
    A2N   alarm  -> normal     the alarm returned to normal

Everything downstream (bad actors, chattering, floods, RCA) counts *alarm
occurrences presented to an operator*, so the log has to be turned back into
episodes: a raise, and the clear that closes it.

The wrinkle in real data: the log is not balanced. This export has 91k A2N rows
but only 4.5k N2A rows — the raises were pruned, or happened before the export
window. Dropping A2N would throw away 89% of the alarm history; treating every
row as an occurrence would double-count the balanced ones. So:

  * N2A            -> opens an episode (ACTIVE)
  * A2A            -> ACTIVE (a fresh presentation to the operator; this is what
                      makes chattering visible)
  * A2N            -> closes the open episode (CLEARED). If nothing is open, the
                      raise was never logged, so we synthesise the ACTIVE at the
                      same timestamp and mark the episode `implicit`.

`implicit` episodes have a known-zero duration and are excluded from
time-to-clear statistics — they are counted, not timed. That distinction is
carried through so no KPI silently averages in a fake 0-minute alarm.
"""
from __future__ import annotations

import pandas as pd

RAISE, RERAISE, CLEAR = "N2A", "A2A", "A2N"


def reconstruct_episodes(tx: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Turn a transition log into (events, episodes).

    `tx` needs: ts, tag, level, priority, transition, description, zone.

    events   — one row per ACTIVE / CLEARED event, the shape the storage layer
               and the whole analysis engine expect.
    episodes — one row per alarm episode with start/end/duration, used for
               standing-alarm and time-to-clear KPIs.
    """
    tx = tx.sort_values("ts", kind="stable")
    events: list[dict] = []
    episodes: list[dict] = []

    for (tag, level), g in tx.groupby(["tag", "level"], sort=False):
        open_ep: dict | None = None
        for r in g.itertuples():
            base = {"ts": r.ts, "tag": tag, "level": level,
                    "priority": r.priority, "value": None, "incident_id": None,
                    "description": r.description, "zone": r.zone}

            if r.transition in (RAISE, RERAISE):
                if open_ep is not None and r.transition == RERAISE:
                    # re-alarm while still active: close the previous leg so the
                    # episode table stays consistent, then open a new one.
                    _close(episodes, open_ep, r.ts)
                events.append({**base, "state": "ACTIVE"})
                open_ep = {"tag": tag, "level": level, "priority": r.priority,
                           "zone": r.zone, "description": r.description,
                           "start_ts": r.ts, "implicit": False}

            elif r.transition == CLEAR:
                if open_ep is None:
                    # raise not present in the log -> synthesise it
                    events.append({**base, "state": "ACTIVE"})
                    open_ep = {"tag": tag, "level": level, "priority": r.priority,
                               "zone": r.zone, "description": r.description,
                               "start_ts": r.ts, "implicit": True}
                events.append({**base, "state": "CLEARED"})
                _close(episodes, open_ep, r.ts)
                open_ep = None

        if open_ep is not None:      # never cleared -> standing alarm
            _close(episodes, open_ep, pd.NaT)

    ev = pd.DataFrame(events).sort_values("ts").reset_index(drop=True)
    ep = pd.DataFrame(episodes).sort_values("start_ts").reset_index(drop=True)
    return ev, ep


def _close(episodes: list[dict], ep: dict, end_ts) -> None:
    dur = (None if pd.isna(end_ts) or ep["implicit"]
           else (end_ts - ep["start_ts"]).total_seconds() / 60.0)
    episodes.append({**ep, "end_ts": end_ts, "duration_min": dur,
                     "standing": bool(pd.isna(end_ts))})
