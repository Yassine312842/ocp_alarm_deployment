"""
Run the analysis engine over stored data and print a report.

    python scripts/run_analysis.py

Works on whatever is in the store — synthetic (ground truth available, so the
root-cause accuracy check runs) or a real alarm log (no truth labels; the KPI
section is what matters instead).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis import engine, kpis
from src.rootcause import engine as rc
from src.storage.duckdb_store import DuckStore

pd.set_option("display.max_rows", 30)
pd.set_option("display.width", 130)
pd.set_option("display.max_colwidth", 46)


def section(title: str):
    print("\n" + "=" * 76 + f"\n  {title}\n" + "=" * 76)


def main():
    t0 = time.time()
    store = DuckStore()
    alarms = store.alarms_df()
    episodes = store.episodes_df()
    meta = store.meta()
    if alarms.empty:
        print("No data. Run scripts/ingest_alarm_log.py (real) "
              "or scripts/generate_demo_data.py (synthetic) first.")
        return
    alarms["ts"] = pd.to_datetime(alarms["ts"])

    section("DATASET")
    active = alarms[alarms.state == "ACTIVE"]
    print(f"  source        : {meta.get('source', 'synthetic generator')}")
    print(f"  activations   : {len(active):,} on {active.tag.nunique():,} sources")
    print(f"  range         : {active.ts.min()} -> {active.ts.max()}")

    section("LAYER 3 · EEMUA-191 performance KPIs")
    rate = kpis.alarm_rate(alarms)
    print(f"  avg alarms / 10 min      : {rate['avg_per_window']}   "
          f"(EEMUA target <= 1.0)  -> {rate['eemua_verdict']}")
    print(f"  peak alarms / 10 min     : {rate['peak_per_window']}   "
          f"(manageable peak <= 10)")
    print(f"  10-min windows in flood  : {rate['flood_windows']:,} of "
          f"{rate['active_windows']:,} active ({rate['flood_share_of_active_pct']}%)")
    ttc = kpis.time_to_clear(episodes)
    if ttc.get("timed_episodes"):
        print(f"  time to clear (median)   : {ttc['median_min']:,} min   "
              f"p90 {ttc['p90_min']:,} min   >24h: {ttc['over_24h_pct']}%")
    print("\n  Priority mix vs ISA-18.2 rationalisation target:")
    print(kpis.priority_distribution(alarms).to_string(index=False))

    section("LAYER 3 · Bad actors (Pareto)")
    print(engine.bad_actors(alarms).to_string(index=False))

    section("LAYER 3 · Chattering instruments")
    ch = engine.chattering(alarms)
    print(ch.head(10).to_string(index=False) if len(ch) else "None detected.")

    section("LAYER 3 · Alarm floods (EEMUA-191: >10 / 10 min)")
    fl = engine.alarm_floods(alarms)
    print(f"  {len(fl):,} flood windows; worst:")
    print(fl.sort_values("alarm_count", ascending=False).head(5).to_string(index=False)
          if len(fl) else "  none")

    section("LAYER 3 · Standing alarms (raised, never cleared)")
    st = kpis.standing_alarms(episodes)
    print(st.head(8).to_string(index=False) if len(st) else "None.")

    section("LAYER 3 · Co-occurring alarm pairs")
    print(engine.co_occurrence(alarms).head(8).to_string(index=False))

    section("LAYER 3 · Frequent pre-incident sequences")
    sq = engine.pre_incident_sequences(alarms)
    print(sq.to_string(index=False) if len(sq) else "None.")

    section("LAYER 4 · Incidents & root-cause ranking")
    inc = rc.incidents(alarms)
    print(f"  {len(inc):,} incidents segmented from the alarm stream\n")
    ranked = rc.rank_candidates(alarms)
    biggest = inc.sort_values("alarm_count", ascending=False).head(3)["incident_id"]
    for i in biggest:
        row = inc[inc.incident_id == i].iloc[0]
        print(f"  {i}  {row.start_ts}  ·  {row.alarm_count} alarms / "
              f"{row.tags} sources  ·  worst {row.top_priority}")
        r = ranked[ranked.incident_id == i].head(3)
        for c in r.itertuples():
            print(f"     #{c.rank} {c.tag[:44]:<44} {c.level:<16} "
                  f"conf {c.confidence:.2f}")
            print(f"         {c.explanation}")
            if c.hint:
                print(f"         hint: {c.hint}")
        print()

    gt_path = Path("ground_truth.json")
    if gt_path.exists() and meta.get("kind") != "real":
        section("LAYER 4 · Root-cause accuracy vs ground truth (synthetic only)")
        print(rc.validate(ranked, json.loads(gt_path.read_text(encoding="utf-8"))))

    print(f"\n[analysis completed in {time.time() - t0:.1f}s]")


if __name__ == "__main__":
    main()
