"""
Layer 2 storage — embedded DuckDB.

Zero-config so you can run today with no server. The schema mirrors what you'd
create in TimescaleDB (see schema.sql), and the queries are plain SQL, so the
move to TimescaleDB later is mostly a connection-string change.

`_migrate` keeps an existing .duckdb file usable after the schema grew the
`description` / `zone` columns (real alarm logs carry operator-facing text and a
console/area, which the synthetic source never had).
"""
from __future__ import annotations

import duckdb
import pandas as pd

from src.acquisition.base import AlarmEvent, ProcessSample

ALARM_COLS = ["ts", "tag", "level", "priority", "state", "value",
              "incident_id", "description", "zone"]


class DuckStore:
    def __init__(self, path: str = "ocp_alarms.duckdb"):
        self.con = duckdb.connect(path)
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS process_samples (
                ts TIMESTAMP, tag VARCHAR, value DOUBLE);
            CREATE TABLE IF NOT EXISTS alarms (
                ts TIMESTAMP, tag VARCHAR, level VARCHAR, priority VARCHAR,
                state VARCHAR, value DOUBLE, incident_id VARCHAR,
                description VARCHAR, zone VARCHAR);
            CREATE TABLE IF NOT EXISTS episodes (
                tag VARCHAR, level VARCHAR, priority VARCHAR, zone VARCHAR,
                description VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
                duration_min DOUBLE, implicit BOOLEAN, standing BOOLEAN);
            CREATE TABLE IF NOT EXISTS dataset_meta (
                key VARCHAR PRIMARY KEY, value VARCHAR);
        """)

    def _migrate(self) -> None:
        cols = {r[0] for r in self.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'alarms'").fetchall()}
        for col in ("description", "zone"):
            if col not in cols:
                self.con.execute(f"ALTER TABLE alarms ADD COLUMN {col} VARCHAR")

    # -- writes ---------------------------------------------------------- #
    def reset(self) -> None:
        self.con.execute("DELETE FROM process_samples; DELETE FROM alarms; "
                         "DELETE FROM episodes; DELETE FROM dataset_meta;")

    def write_samples(self, samples: list[ProcessSample]) -> None:
        if not samples:
            return
        df = pd.DataFrame([s.__dict__ for s in samples])
        self.con.register("df_s", df)
        self.con.execute("INSERT INTO process_samples SELECT ts, tag, value FROM df_s")
        self.con.unregister("df_s")

    def write_alarms(self, alarms: list[AlarmEvent]) -> None:
        if not alarms:
            return
        df = pd.DataFrame([a.__dict__ for a in alarms])
        self.write_alarms_df(df)

    def write_alarms_df(self, df: pd.DataFrame) -> None:
        """Bulk path — the real loader already has a DataFrame; going back
        through 191k dataclasses just to build one again is pure overhead."""
        df = df.reindex(columns=ALARM_COLS)
        self.con.register("df_a", df)
        self.con.execute(f"INSERT INTO alarms SELECT {', '.join(ALARM_COLS)} FROM df_a")
        self.con.unregister("df_a")

    def write_episodes(self, df: pd.DataFrame) -> None:
        cols = ["tag", "level", "priority", "zone", "description",
                "start_ts", "end_ts", "duration_min", "implicit", "standing"]
        df = df.reindex(columns=cols)
        self.con.register("df_e", df)
        self.con.execute(f"INSERT INTO episodes SELECT {', '.join(cols)} FROM df_e")
        self.con.unregister("df_e")

    def set_meta(self, **kv: str) -> None:
        for k, v in kv.items():
            self.con.execute("INSERT OR REPLACE INTO dataset_meta VALUES (?, ?)",
                             [k, str(v)])

    # -- reads ----------------------------------------------------------- #
    def meta(self) -> dict:
        try:
            rows = self.con.execute("SELECT key, value FROM dataset_meta").fetchall()
        except duckdb.CatalogException:
            return {}
        return dict(rows)

    def alarms_df(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM alarms ORDER BY ts").df()

    def episodes_df(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM episodes ORDER BY start_ts").df()

    def samples_df(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM process_samples ORDER BY ts").df()

    def query(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).df()
