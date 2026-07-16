"""
Layer 2 — Preparation.

The hard part of prep is temporal alignment: alarms are discrete events,
process tags are continuous samples. `align_tags` pivots samples onto a common
resampled grid; `flag_outliers` adds a robust (MAD-based) outlier column.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def align_tags(samples: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """Long -> wide, resampled onto a common time grid (one column per tag)."""
    wide = (samples.pivot_table(index="ts", columns="tag", values="value",
                                aggfunc="mean")
            .sort_index().resample(freq).mean().interpolate(limit=3))
    return wide


def flag_outliers(samples: pd.DataFrame, z: float = 4.0) -> pd.DataFrame:
    """Robust outlier flag per tag using median absolute deviation."""
    out = samples.copy()
    def _flag(g):
        med = g["value"].median()
        mad = (g["value"] - med).abs().median() or 1e-9
        g["is_outlier"] = ((g["value"] - med).abs() / (1.4826 * mad)) > z
        return g
    return out.groupby("tag", group_keys=False).apply(_flag)
