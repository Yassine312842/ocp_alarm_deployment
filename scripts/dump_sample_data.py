"""
Refresh the dashboard's offline fallback.

    python scripts/dump_sample_data.py

Writes frontend/sample_data.json AND rewrites the `window.EMBEDDED = {...}` line
inside frontend/index.html, so the dashboard opens standalone (SAMPLE DATA badge)
with the real analysed numbers. Previously the embedded copy had to be pasted in
by hand, which is how it drifted from the JSON.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis import engine, kpis
from src.rootcause import engine as rc
from src.storage.duckdb_store import DuckStore

ROOT = Path(__file__).resolve().parents[1]
TOP_INCIDENTS = 12          # keep the embedded payload small enough to inline


def main():
    store = DuckStore()
    a = store.alarms_df()
    episodes = store.episodes_df()
    meta = store.meta()
    if a.empty:
        print("No data. Run scripts/ingest_alarm_log.py or generate_demo_data.py first.")
        return
    a["ts"] = pd.to_datetime(a["ts"])
    if not episodes.empty:
        episodes["start_ts"] = pd.to_datetime(episodes["start_ts"])
    active = a[a["state"] == "ACTIVE"]

    ba = engine.bad_actors(active, top=10)
    floods = engine.alarm_floods(active)
    floods["window_start"] = floods["window_start"].astype(str)

    order = engine.PRIORITY_ORDER
    inv = {v: k for k, v in order.items()}
    ts_rows = (active.assign(_o=active["priority"].map(order).fillna(0))
               .groupby("tag").agg(alarm_count=("tag", "size"), _o=("_o", "max"),
                                   last_ts=("ts", "max")).reset_index())
    ts_rows["worst_priority"] = ts_rows["_o"].map(inv).fillna("LOW")
    ts_rows["last_ts"] = ts_rows["last_ts"].astype(str)
    ts_rows = ts_rows.sort_values("alarm_count", ascending=False).head(60)

    inc = rc.incidents(active)
    ranked = rc.rank_candidates(active)
    keep = set(inc.sort_values("alarm_count", ascending=False)
               .head(TOP_INCIDENTS)["incident_id"]) if len(inc) else set()
    rk = ranked[ranked["incident_id"].isin(keep)].copy()
    if len(rk):
        rk["first_ts"] = rk["first_ts"].astype(str)

    payload = {
        "summary": {
            "total_alarms": int(len(active)),
            "by_priority": active["priority"].value_counts().to_dict(),
            "incidents": int(len(inc)),
            "sources": int(active["tag"].nunique()),
            "bad_actor_share": float(ba["pct"].iloc[0]) if len(ba) else 0.0,
            "bad_actor_tag": str(ba["tag"].iloc[0]) if len(ba) else None,
            "flood_windows": int(len(floods)),
            "range": [str(active["ts"].min()), str(active["ts"].max())],
            "dataset": meta.get("source", "synthetic generator"),
            "kind": meta.get("kind", "synthetic"),
        },
        "kpis": kpis.summary_kpis(active, episodes),
        "tags_status": ts_rows[["tag", "alarm_count", "worst_priority",
                                "last_ts"]].to_dict("records"),
        "bad_actors": ba.to_dict("records"),
        "chattering": engine.chattering(active, top=15).to_dict("records"),
        "floods": floods.to_dict("records"),
        "co_occurrence": engine.co_occurrence(active, top=20).to_dict("records"),
        "sequences": engine.pre_incident_sequences(active).to_dict("records"),
        "incidents": (lambda d: [dict(r, start_ts=str(r["start_ts"]),
                                      end_ts=str(r["end_ts"])) for r in d])(
            inc.sort_values("alarm_count", ascending=False)
               .head(60).to_dict("records")) if len(inc) else [],
        "root_cause": rk.to_dict("records"),
        "cause_hints": rc.cause_hints(ranked) if len(ranked) else {},
    }

    # recommendations for the embedded sample (offline mode) — no KB matches yet,
    # so these come from the playbook + incident structure.
    try:
        from src.rootcause import recommend as _reco
        co = engine.co_occurrence(active, top=60)
        ch = engine.chattering(active, top=60)
        rec_ids = (inc.sort_values("alarm_count", ascending=False)
                   .head(12)["incident_id"].tolist()) if len(inc) else []
        recs = []
        for iid in rec_ids:
            rws = ranked[ranked["incident_id"] == iid].sort_values("rank")
            if rws.empty:
                continue
            t = rws.iloc[0]
            z = None
            if len(inc) and "zone" in inc:
                zz = inc.loc[inc["incident_id"] == iid, "zone"]
                z = None if zz.empty else zz.iloc[0]
            sig = {"root_tag": t["tag"],
                   "alarm_types": sorted(rws["level"].unique().tolist()), "zone": z}
            recs.append({"incident_id": iid, "signature": sig, "known_case": False,
                         "recommendations": _reco.recommend(iid, ranked,
                             co_occurrence=co, chattering=ch, kb_match=None)})
        payload["recommendations"] = recs
        payload["kb_stats"] = {"total": 0, "distinct_signatures": 0,
                               "recurring": 0,
                               "engine_accuracy": {"confirmations": 0,
                                                   "top1_correct": 0, "accuracy": None}}
    except Exception as e:  # never let the sample dump fail on the extras
        payload["recommendations"] = []
        payload["kb_stats"] = {"total": 0, "recurring": 0}
        print(f"  (recommendations skipped: {e})")

    blob = json.dumps(payload, default=str)
    out = ROOT / "frontend" / "sample_data.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    index = ROOT / "frontend" / "index.html"
    html = index.read_text(encoding="utf-8")
    new = f"<script>window.EMBEDDED = {blob};</script>"
    html, n = re.subn(r"<script>window\.EMBEDDED = .*?;</script>", lambda _: new,
                      html, count=1, flags=re.S)
    if not n:
        print("!! window.EMBEDDED block not found in index.html — not updated")
    else:
        index.write_text(html, encoding="utf-8")

    print(f"Wrote {out.relative_to(ROOT)} and re-embedded it in "
          f"{index.relative_to(ROOT)} ({len(blob):,} bytes)")


if __name__ == "__main__":
    main()
