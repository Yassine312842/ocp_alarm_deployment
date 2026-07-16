"""
Layer 4 — Root cause.

The synthetic version leaned on `incident_id`: the generator planted the
incidents, so grouping was free. Real alarm logs have no incident column — the
first job is to *find* the incidents, and only then rank causes inside them.

    segment_incidents()  gap-split the alarm stream into episodes, keep the ones
                         that look like an upset (enough alarms, enough sources,
                         at least one significant alarm)
    rank_candidates()    within each incident, rank tags as probable causes
    validate()           accuracy against synthetic ground truth (dev only)

Ranking stays deliberately explainable. Two signals, both auditable by an
engineer who does not trust the model:

  1. **Order.** Causes precede effects, so the earliest alarm in the incident
     ranks first. This alone is what the demo did.
  2. **Initiation rate.** Across the whole corpus, how often does this tag
     *open* an incident it takes part in? A tag that appears in 40 incidents and
     starts 38 of them is behaving like a cause. A tag that appears in 40 and
     starts 2 is a follower — it is downstream, and ranking it first because it
     happened to fire early once is exactly the error a plain sort makes.

Confidence blends the two. That is the ceiling for a heuristic; the honest next
step is Granger / transfer-entropy or a causal-discovery pass over the process
tags, which needs the continuous historian feed that this dataset does not have.
"""
from __future__ import annotations

import pandas as pd

from src.acquisition.alarm_log import TYPE_CAUSE_HINT


def segment_incidents(alarms: pd.DataFrame, gap_min: int = 15,
                      min_alarms: int = 3, min_tags: int = 2,
                      priorities: tuple[str, ...] = ("CRITICAL", "HIGH"),
                      ) -> pd.DataFrame:
    """Split the ACTIVE stream into incidents and label each alarm with one.

    Returns the ACTIVE alarms with an `incident_id` column (NaN where the alarm
    is not part of a qualifying incident). An incident is a burst of alarms with
    no `gap_min` silence inside it, involving at least `min_tags` distinct
    sources, containing at least one alarm of a significant priority. The
    thresholds are the knobs an alarm engineer will actually want to turn, so
    they are arguments, not constants.
    """
    a = alarms[alarms["state"] == "ACTIVE"].sort_values("ts").reset_index(drop=True)
    if a.empty:
        return a.assign(incident_id=pd.Series(dtype="object"))
    gap = a["ts"].diff().dt.total_seconds().fillna(0)
    ep = (gap > gap_min * 60).cumsum()
    stats = a.assign(_ep=ep).groupby("_ep").agg(
        n=("tag", "size"), tags=("tag", "nunique"),
        sig=("priority", lambda s: s.isin(priorities).any()))
    ok = stats[(stats["n"] >= min_alarms) & (stats["tags"] >= min_tags)
               & (stats["sig"])].index
    ids = {e: f"INC-{i:05d}" for i, e in enumerate(sorted(ok))}
    a["incident_id"] = pd.Series(ep).map(ids)
    return a


def _labelled(alarms: pd.DataFrame, **kw) -> pd.DataFrame:
    """ACTIVE alarms carrying an incident_id — the given one if the data already
    has incidents (synthetic ground truth), otherwise discovered ones."""
    a = alarms[alarms["state"] == "ACTIVE"].copy()
    if a.empty:
        return a
    if "incident_id" in a and a["incident_id"].notna().any():
        return a[a["incident_id"].notna()]
    return segment_incidents(a, **kw).dropna(subset=["incident_id"])


def incidents(alarms: pd.DataFrame, **kw) -> pd.DataFrame:
    """One row per incident: when, how big, how bad, which console."""
    a = _labelled(alarms, **kw)
    if a.empty:
        return pd.DataFrame(columns=["incident_id", "start_ts", "end_ts",
                                     "alarm_count", "tags", "top_priority", "zone"])
    from src.analysis.engine import PRIORITY_ORDER
    g = a.groupby("incident_id")
    out = g.agg(start_ts=("ts", "min"), end_ts=("ts", "max"),
                alarm_count=("tag", "size"), tags=("tag", "nunique")).reset_index()
    worst = (a.assign(_o=a["priority"].map(PRIORITY_ORDER).fillna(0))
             .sort_values("_o", ascending=False).drop_duplicates("incident_id")
             .set_index("incident_id"))
    out["top_priority"] = out["incident_id"].map(worst["priority"])
    if "zone" in a:
        out["zone"] = out["incident_id"].map(worst["zone"])
    out["duration_min"] = ((out["end_ts"] - out["start_ts"])
                           .dt.total_seconds() / 60).round(1)
    return out.sort_values("start_ts").reset_index(drop=True)


def initiation_rates(labelled: pd.DataFrame) -> pd.DataFrame:
    """Per tag: how often it *opens* an incident it appears in.

    This is the whole difference between "fired first once" and "behaves like a
    cause". It is computed over the corpus, so a single incident cannot invent
    a cause on its own.
    """
    a = labelled[labelled["incident_id"].notna()]
    if a.empty:
        return pd.DataFrame(columns=["tag", "appearances", "initiations",
                                     "initiation_rate"])
    # This log is minute-resolution, so an upset can put 50 tags on the same
    # timestamp. Crediting only one of them as "the initiator" is an artefact of
    # sort order, not a fact about the plant: every tag tied at the incident's
    # first timestamp counts as having opened it.
    firsts = a.groupby(["incident_id", "tag"])["ts"].min().reset_index()
    t0 = firsts.groupby("incident_id")["ts"].transform("min")
    first = (firsts.loc[firsts["ts"] == t0, "tag"]
             .value_counts().rename("initiations"))
    appear = (a.groupby("tag")["incident_id"].nunique().rename("appearances"))
    out = pd.concat([appear, first], axis=1).fillna(0).reset_index(names="tag")
    out["initiation_rate"] = (out["initiations"] / out["appearances"]).round(3)
    # A tag seen in one incident, which it opened, has an observed initiation
    # rate of 100% — and that number is worth nothing. Shrink toward the corpus
    # base rate so confidence has to be *earned* by repeated observation:
    # a tag needs several appearances before its own history outweighs the prior.
    base = float(out["initiations"].sum() / max(out["appearances"].sum(), 1))
    prior_strength = 5.0
    out["initiation_rate_smoothed"] = (
        (out["initiations"] + prior_strength * base)
        / (out["appearances"] + prior_strength)).round(3)
    return out.sort_values("initiations", ascending=False).reset_index(drop=True)


def rank_candidates(alarms: pd.DataFrame, top_per_incident: int = 6,
                    **seg_kw) -> pd.DataFrame:
    """Rank probable root-cause tags within each incident.

    Works on both data shapes: if the alarms already carry an `incident_id`
    (synthetic ground truth), that grouping is used as-is; otherwise incidents
    are discovered first. Downstream code — the API, the dashboard — does not
    need to know which case it is in.
    """
    cols = ["incident_id", "rank", "tag", "level", "priority", "first_ts",
            "lead_min", "initiation_rate", "confidence", "explanation", "hint"]
    a = alarms[alarms["state"] == "ACTIVE"].copy()
    if a.empty:
        return pd.DataFrame(columns=cols)

    labelled = _labelled(a, **seg_kw)
    if labelled.empty:
        return pd.DataFrame(columns=cols)

    rates = initiation_rates(labelled).set_index("tag")

    first = (labelled.sort_values("ts")
             .groupby(["incident_id", "tag"], as_index=False)
             .agg(first_ts=("ts", "min"), level=("level", "first"),
                  priority=("priority", "first"), alarms=("tag", "size")))
    first = first.sort_values(["incident_id", "first_ts"])
    t0 = first.groupby("incident_id")["first_ts"].transform("min")
    t1 = first.groupby("incident_id")["first_ts"].transform("max")
    span = (t1 - t0).dt.total_seconds().replace(0, pd.NA)
    # 1.0 for the first tag in the incident, 0.0 for the last
    lead_share = (1 - ((first["first_ts"] - t0).dt.total_seconds() / span)).fillna(1.0)
    first["lead_min"] = ((first["first_ts"] - t0).dt.total_seconds() / 60).round(1)
    first["initiation_rate"] = first["tag"].map(rates["initiation_rate"]).fillna(0.0)
    smoothed = first["tag"].map(rates["initiation_rate_smoothed"]).fillna(0.0)
    first["appearances"] = first["tag"].map(rates["appearances"]).fillna(0).astype(int)
    first["confidence"] = (0.6 * smoothed + 0.4 * lead_share).round(3)

    # Rank on confidence, not on raw order. Sorting purely by timestamp puts
    # whichever downstream point happened to trip first at the top; the corpus
    # knows which of these tags actually behaves like an initiator.
    first = first.sort_values(["incident_id", "confidence", "first_ts"],
                              ascending=[True, False, True])
    first["rank"] = first.groupby("incident_id").cumcount() + 1

    first["explanation"] = [
        f"fired {'first' if r.lead_min == 0 else f'{r.lead_min:g} min into the incident'}; "
        f"opens {r.initiation_rate:.0%} of the {r.appearances} incidents it appears in"
        for r in first.itertuples()]
    # Limit alarms (LO/HI/...) from the synthetic source have no type hint; the
    # knowledge base is what fills that gap, so leave it empty rather than fake it.
    hints = first["level"].map(TYPE_CAUSE_HINT)
    first["hint"] = hints.where(hints.notna(), None)

    out = first.sort_values(["incident_id", "rank"])
    out = out.groupby("incident_id").head(top_per_incident)
    return out[cols].reset_index(drop=True)


def cause_hints(ranked: pd.DataFrame) -> dict[str, str]:
    """tag -> plain-language probable cause, for the dashboard / knowledge base.

    A placeholder for the Neo4j knowledge graph: today the hint comes from the
    alarm type, tomorrow from operator-confirmed cases in the KB.
    """
    if ranked.empty:
        return {}
    top = ranked[ranked["rank"] == 1]
    return {r.tag: (r.hint or f"Investigate {r.tag}") for r in top.itertuples()}


def validate(ranked: pd.DataFrame, ground_truth: list[dict]) -> dict:
    """Did the #1 candidate tag match the scenario's known root-cause tag?
    Synthetic data only — real data has no truth label until an operator
    confirms one, which is what the knowledge base is for."""
    from config import SCENARIOS
    truth_tag = {s.key: s.chain[0][0] for s in SCENARIOS}
    gt = {g["incident_id"]: truth_tag[g["scenario"]] for g in ground_truth}
    top1 = ranked[ranked["rank"] == 1].set_index("incident_id")["tag"].to_dict()
    hits = sum(1 for inc, tag in top1.items() if gt.get(inc) == tag)
    n = len(top1)
    return {"incidents": n, "top1_correct": hits,
            "accuracy": round(hits / n, 3) if n else 0.0}
