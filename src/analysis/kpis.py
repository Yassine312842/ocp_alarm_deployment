"""
Layer 3 — EEMUA-191 / ISA-18.2 performance KPIs.

This folds in (and fixes) the old `src/analytics/eemua_kpis.py`. That version
assumed a small, dense frame: `groupby(operator).resample("10min")` materialises
*every* window in each operator's time range, so on a 9-year log across 157
consoles it tries to build ~70M rows of mostly zeros before computing a mean.
Here the same metrics are computed on occupied windows only, and the denominator
is stated explicitly rather than implied by the index.

Metrics, and why each one is here:

  * **alarm rate** — EEMUA's headline. Target is an average of ≤1 alarm per
    operator per 10 min, with ≤10 in 10 min still "manageable". Anything above
    that is, in EEMUA's language, "likely to be unacceptable".
  * **priority distribution** — ISA-18.2's rationalisation target is roughly
    80% low / 15% medium / 5% high+critical. A system where most alarms are
    "high" has no priority at all.
  * **standing alarms** — alarms active for days. They are wallpaper: they train
    operators to ignore the annunciator.
  * **time to clear** — how long the average alarm sits on the screen.
  * **flood share** — proportion of operating time spent in an alarm flood.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

EEMUA_TARGET_RATE_PER_10MIN = 1.0
EEMUA_MANAGEABLE_PEAK_PER_10MIN = 10.0
STANDING_ALARM_HOURS = 24
# ISA-18.2 / EEMUA-191 rationalisation target, as a share of total alarms.
TARGET_PRIORITY_MIX = {"CRITICAL": 1.0, "HIGH": 4.0, "MEDIUM": 15.0, "LOW": 80.0}


def _active(alarms: pd.DataFrame) -> pd.DataFrame:
    return alarms[alarms["state"] == "ACTIVE"]


def alarm_rate(alarms: pd.DataFrame, window_min: int = 10) -> dict:
    """System-wide alarm rate against the EEMUA-191 bands."""
    a = _active(alarms)
    if a.empty:
        return {}
    span_min = (a["ts"].max() - a["ts"].min()).total_seconds() / 60.0
    windows = max(span_min / window_min, 1)
    per_window = a.groupby(a["ts"].dt.floor(f"{window_min}min")).size()
    flood = int((per_window > EEMUA_MANAGEABLE_PEAK_PER_10MIN).sum())
    return {
        "window_min": window_min,
        "avg_per_window": round(len(a) / windows, 2),
        "avg_per_window_when_active": round(float(per_window.mean()), 2),
        "peak_per_window": int(per_window.max()),
        "active_windows": int(len(per_window)),
        "flood_windows": flood,
        "flood_share_of_active_pct": round(100 * flood / len(per_window), 1),
        "eemua_verdict": _verdict(len(a) / windows),
        "target_per_window": EEMUA_TARGET_RATE_PER_10MIN,
    }


def _verdict(rate: float) -> str:
    if rate <= EEMUA_TARGET_RATE_PER_10MIN:
        return "within EEMUA target"
    if rate <= EEMUA_MANAGEABLE_PEAK_PER_10MIN:
        return "above target, still manageable"
    return "unacceptable per EEMUA-191"


def priority_distribution(alarms: pd.DataFrame) -> pd.DataFrame:
    """Actual vs. rationalisation-target priority mix."""
    a = _active(alarms)
    counts = a["priority"].value_counts()
    total = int(counts.sum()) or 1
    rows = []
    for p, target in TARGET_PRIORITY_MIX.items():
        n = int(counts.get(p, 0))
        rows.append({"priority": p, "alarm_count": n,
                     "pct": round(100 * n / total, 1),
                     "target_pct": target,
                     "delta_pct": round(100 * n / total - target, 1)})
    return pd.DataFrame(rows)


def zone_load(alarms: pd.DataFrame, window_min: int = 10,
              top: int = 10) -> pd.DataFrame:
    """Alarm load per console/zone — EEMUA rates are defined *per operator*."""
    a = _active(alarms)
    if a.empty or "zone" not in a:
        return pd.DataFrame(columns=["zone", "alarm_count", "peak_per_window"])
    a = a.assign(_w=a["ts"].dt.floor(f"{window_min}min"))
    per = a.groupby(["zone", "_w"]).size().rename("n").reset_index()
    out = (per.groupby("zone")
           .agg(alarm_count=("n", "sum"), peak_per_window=("n", "max"),
                active_windows=("n", "size"))
           .reset_index().sort_values("alarm_count", ascending=False))
    out["over_manageable_peak"] = out["peak_per_window"] > EEMUA_MANAGEABLE_PEAK_PER_10MIN
    return out.head(top).reset_index(drop=True)


def standing_alarms(episodes: pd.DataFrame, top: int = 15,
                    hours: int = STANDING_ALARM_HOURS) -> pd.DataFrame:
    """Alarms that were raised and never returned to normal in the log.

    An alarm standing for weeks is not information, it is furniture — and it is
    the single easiest thing to hand an engineer on day one.
    """
    cols = ["tag", "level", "priority", "start_ts", "days_standing", "description"]
    if episodes is None or episodes.empty or "standing" not in episodes:
        return pd.DataFrame(columns=cols)
    s = episodes[episodes["standing"]].copy()
    if s.empty:
        return pd.DataFrame(columns=cols)
    as_of = episodes["start_ts"].max()
    s["days_standing"] = ((as_of - s["start_ts"]).dt.total_seconds() / 86400).round(1)
    s = s[s["days_standing"] >= hours / 24]
    return (s.sort_values("days_standing", ascending=False)
            .head(top)[cols].reset_index(drop=True))


def time_to_clear(episodes: pd.DataFrame) -> dict:
    """Distribution of how long an alarm stays on the operator's screen.

    Only *timed* episodes count — the ones where both the raise and the clear
    are in the log. Episodes whose raise was never logged are counted elsewhere
    but have no duration, and averaging a fabricated 0 into this would flatter
    the number badly (they are 90% of the file).
    """
    if episodes is None or episodes.empty:
        return {}
    timed = episodes[episodes["duration_min"].notna()]
    if timed.empty:
        return {"timed_episodes": 0}
    d = timed["duration_min"]
    return {
        "timed_episodes": int(len(timed)),
        "untimed_episodes": int(len(episodes) - len(timed)),
        "median_min": round(float(d.median()), 1),
        "p90_min": round(float(np.percentile(d, 90)), 1),
        "over_24h_pct": round(100 * float((d > 1440).mean()), 1),
        "under_60s_pct": round(100 * float((d < 1).mean()), 1),
    }


def summary_kpis(alarms: pd.DataFrame, episodes: pd.DataFrame | None = None) -> dict:
    """One call for the KPI tile row on the dashboard."""
    rate = alarm_rate(alarms)
    dist = priority_distribution(alarms)
    high_share = float(dist.loc[dist["priority"].isin(["CRITICAL", "HIGH"]),
                                "pct"].sum())
    ttc = time_to_clear(episodes) if episodes is not None else {}
    standing = standing_alarms(episodes) if episodes is not None else pd.DataFrame()
    return {
        "rate": rate,
        "priority_mix": dist.to_dict("records"),
        "high_priority_share_pct": round(high_share, 1),
        "high_priority_target_pct": TARGET_PRIORITY_MIX["CRITICAL"] + TARGET_PRIORITY_MIX["HIGH"],
        "time_to_clear": ttc,
        "standing_alarm_count": int(episodes["standing"].sum()) if episodes is not None
                                 and "standing" in episodes else 0,
        "standing_alarms": standing.assign(
            start_ts=lambda d: d["start_ts"].astype(str)).to_dict("records")
            if len(standing) else [],
        "zone_load": zone_load(alarms).to_dict("records"),
    }
