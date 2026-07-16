"""
Populate the local store with synthetic data.

    python scripts/generate_demo_data.py

To use REAL data later, replace SyntheticDataSource with a connector from
src/acquisition/historian.py — the rest of the pipeline is unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.synthetic import SyntheticDataSource
from src.storage.duckdb_store import DuckStore


def main():
    print("Generating synthetic MAP-line data ...")
    src = SyntheticDataSource(days=14, incidents_per_day=1.4)
    samples = src.process_samples()
    alarms = src.alarm_events()

    store = DuckStore()
    store.reset()
    store.write_samples(samples)
    store.write_alarms(alarms)

    # persist ground truth for validation (synthetic only)
    import json
    gt = src.incident_ground_truth()
    for g in gt:
        g["t0"] = g["t0"].isoformat()
    Path("ground_truth.json").write_text(json.dumps(gt, indent=2), encoding="utf-8")

    print(f"  process samples: {len(samples):,}")
    print(f"  alarm events   : {len(alarms):,}")
    print(f"  incidents      : {len(gt)}")
    print("Stored in ocp_alarms.duckdb (+ ground_truth.json). Run run_analysis.py next.")


if __name__ == "__main__":
    main()
