"""
Load a REAL historian alarm-log export into the store.

    python scripts/ingest_alarm_log.py [csv_path]

This is the swap the skeleton was built for: `AlarmLogDataSource` replaces
`SyntheticDataSource` and nothing downstream changes. Run `run_analysis.py` or
start the API afterwards exactly as before.

Default input: data/preprocessed_trendedpointalarm.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.alarm_log import AlarmLogDataSource
from src.storage.duckdb_store import DuckStore

DEFAULT_CSV = "data/preprocessed_trendedpointalarm.csv"


def main():
    csv = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
    if not csv.exists():
        sys.exit(f"No such file: {csv}\nUsage: python scripts/ingest_alarm_log.py [csv]")

    print(f"Reading {csv} ...")
    src = AlarmLogDataSource(csv)
    events, episodes, tx = src.events_df(), src.episodes_df(), src.transactions_df()

    store = DuckStore()
    store.reset()
    store.write_alarms_df(events)
    store.write_episodes(episodes)
    store.set_meta(kind="real", source=csv.name,
                   ingested_rows=len(tx), rejected_rows=src.rejected,
                   range_start=str(tx["ts"].min()), range_end=str(tx["ts"].max()))

    # ground truth is a synthetic-only artefact; leaving it around would make
    # run_analysis.py "validate" real incidents against a simulator's answers.
    gt = Path("ground_truth.json")
    if gt.exists():
        gt.unlink()
        print("  removed ground_truth.json (synthetic-only)")

    active = int((events["state"] == "ACTIVE").sum())
    print(f"  transactions   : {len(tx):,}  (rejected {src.rejected})")
    print(f"  alarm events   : {len(events):,}  ({active:,} activations)")
    print(f"  episodes       : {len(episodes):,}"
          f"  ({int(episodes['standing'].sum()):,} never cleared)")
    print(f"  sources (tags) : {tx['tag'].nunique():,}   zones: {tx['zone'].nunique()}")
    print(f"  range          : {tx['ts'].min()} -> {tx['ts'].max()}")
    print("Stored in ocp_alarms.duckdb. Run scripts/run_analysis.py next.")


if __name__ == "__main__":
    main()
