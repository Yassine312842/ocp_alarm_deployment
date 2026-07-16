"""
Layer 3 — Analysis engine.

Everything here is grounded in ISA-18.2 / EEMUA-191 alarm management. These are
the high-value, low-complexity analytics that justify the project on day one.
All functions take the alarms DataFrame from storage and return a DataFrame.

Scale note: these were written against 14 days of synthetic data (~3k events).
Real logs are two orders of magnitude bigger and *far* more skewed — one asset
in this export raises 49k alarms on its own. The O(n²) inner loops that were
invisible at demo scale are quadratic on that single tag, so `chattering` is now
vectorised with searchsorted and `co_occurrence` is windowed with an explicit
neighbour cap. Same definitions, same outputs — they just terminate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PRIORITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _active(alarms: pd.DataFrame) -> pd.DataFrame:
    return alarms[alarms["state"] == "ACTIVE"].copy()


def bad_actors(alarms: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """Pareto of alarm sources. Usually ~10 tags cause ~80% of the load."""
    a = _active(alarms)
    counts = (a.groupby("tag").size().sort_values(ascending=False)
              .rename("alarm_count").reset_index())
    if counts.empty:
        return counts
    total = counts["alarm_count"].sum()
    counts["pct"] = (100 * counts["alarm_count"] / total).round(1)
    counts["cum_pct"] = counts["pct"].cumsum().round(1)
    worst = (a.assign(_o=a["priority"].map(PRIORITY_ORDER).fillna(0))
             .sort_values("_o", ascending=False)
             .drop_duplicates("tag").set_index("tag")["priority"])
    counts["worst_priority"] = counts["tag"].map(worst)
    top_lvl = (a.groupby(["tag", "level"]).size().rename("n").reset_index()
               .sort_values("n", ascending=False).drop_duplicates("tag")
               .set_index("tag")["level"])
    counts["top_level"] = counts["tag"].map(top_lvl)
    return counts.head(top)


def chattering(alarms: pd.DataFrame, window_s: int = 60,
               min_repeats: int = 3, top: int = 25) -> pd.DataFrame:
    """Chattering = same alarm re-activating >= min_repeats within window_s.

    ISA-18.2 singles these out because they are almost always an instrument or
    deadband problem, not a process problem — the cheapest alarm load to delete.
    """
    a = _active(alarms).sort_values("ts")
    cols = ["tag", "level", "activations", "chattering_events", "chatter_pct"]
    if a.empty:
        return pd.DataFrame(columns=cols)
    win = np.timedelta64(window_s, "s")
    rows = []
    for (tag, level), g in a.groupby(["tag", "level"], sort=False):
        ts = g["ts"].to_numpy(dtype="datetime64[ns]")
        if len(ts) < min_repeats:
            continue
        # how many events fall inside each event's forward window (self included)
        end = np.searchsorted(ts, ts + win, side="right")
        in_window = end - np.arange(len(ts))
        chatter = int((in_window >= min_repeats).sum())
        if chatter:
            rows.append({"tag": tag, "level": level, "activations": int(len(ts)),
                         "chattering_events": chatter,
                         "chatter_pct": round(100 * chatter / len(ts), 1)})
    out = pd.DataFrame(rows, columns=cols)
    if out.empty:
        return out
    return (out.sort_values("chattering_events", ascending=False)
            .head(top).reset_index(drop=True))


def alarm_floods(alarms: pd.DataFrame, window_min: int = 10,
                 threshold: int = 10) -> pd.DataFrame:
    """EEMUA-191 flood: > threshold alarms per operator per window_min.
    Returns each flood window with its alarm count and dominant priority."""
    cols = ["window_start", "alarm_count", "top_priority"]
    a = _active(alarms).set_index("ts").sort_index()
    if a.empty:
        return pd.DataFrame(columns=cols)
    counts = a["tag"].resample(f"{window_min}min").count()
    floods = counts[counts > threshold]
    if floods.empty:
        return pd.DataFrame(columns=cols)
    grp = a.assign(_b=a.index.floor(f"{window_min}min"))
    grp = grp[grp["_b"].isin(floods.index)]
    top = (grp.groupby(["_b", "priority"]).size().rename("n").reset_index()
           .sort_values("n", ascending=False).drop_duplicates("_b")
           .set_index("_b")["priority"])
    return pd.DataFrame({
        "window_start": list(floods.index),
        "alarm_count": floods.to_numpy().astype(int),
        "top_priority": [top.get(t, "-") for t in floods.index],
    })


def co_occurrence(alarms: pd.DataFrame, window_min: int = 15,
                  min_pairs: int = 3, max_neighbours: int = 40,
                  top: int = 40) -> pd.DataFrame:
    """Tag pairs whose ACTIVE alarms repeatedly fall within window_min of each
    other. A cheap correlation signal that feeds root-cause analysis.

    `max_neighbours` bounds the forward scan. Without it a single 10-minute
    flood of 800 alarms contributes ~320k pairs and the ranking ends up
    describing the flood rather than any relationship between two tags.
    """
    a = _active(alarms).sort_values("ts").reset_index(drop=True)
    if a.empty:
        return pd.DataFrame(columns=["tag_a", "tag_b", "co_occurrences"])
    ts = a["ts"].to_numpy(dtype="datetime64[ns]")
    tags = a["tag"].to_numpy()
    end = np.searchsorted(ts, ts + np.timedelta64(window_min, "m"), side="right")
    pair_counts: dict[tuple[str, str], int] = {}
    for i in range(len(a)):
        hi = min(int(end[i]), i + 1 + max_neighbours)
        ti = tags[i]
        for j in range(i + 1, hi):
            tj = tags[j]
            if ti != tj:
                key = (ti, tj) if ti < tj else (tj, ti)
                pair_counts[key] = pair_counts.get(key, 0) + 1
    rows = [{"tag_a": k[0], "tag_b": k[1], "co_occurrences": v}
            for k, v in pair_counts.items() if v >= min_pairs]
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return (out.sort_values("co_occurrences", ascending=False)
            .head(top).reset_index(drop=True))


def pre_incident_sequences(alarms: pd.DataFrame, top: int = 8,
                           gap_min: int = 30, depth: int = 5,
                           anchor: str = "CRITICAL") -> pd.DataFrame:
    """Frequent ordered alarm sequences that precede an `anchor`-priority alarm.

    Groups the ACTIVE alarm stream into episodes (gap-split), and for every
    episode containing an anchor alarm, records the ordered tag:level sequence
    leading up to it. Real data has no incident_id, so the anchor alarm *is* the
    incident. Frequent sequences are candidate early-warning rules.
    """
    a = _active(alarms).sort_values("ts").reset_index(drop=True)
    if a.empty:
        return pd.DataFrame(columns=["sequence", "occurrences"])
    gap = a["ts"].diff().dt.total_seconds().fillna(0)
    a["_ep"] = (gap > gap_min * 60).cumsum()
    seqs: dict[str, int] = {}
    for _, g in a.groupby("_ep", sort=False):
        hit = np.flatnonzero((g["priority"] == anchor).to_numpy())
        if not len(hit):
            continue
        g = g.iloc[: int(hit[-1]) + 1]      # everything up to and including the trip
        steps = [f"{r.tag}:{r.level}" for r in g.itertuples()]
        deduped = [s for i, s in enumerate(steps) if i == 0 or s != steps[i - 1]]
        if len(deduped) < 2:
            continue
        sig = " -> ".join(deduped[-depth:])
        seqs[sig] = seqs.get(sig, 0) + 1
    rows = [{"sequence": k, "occurrences": v} for k, v in seqs.items()]
    out = pd.DataFrame(rows)
    return (out.sort_values("occurrences", ascending=False).head(top)
            .reset_index(drop=True) if len(out) else out)
