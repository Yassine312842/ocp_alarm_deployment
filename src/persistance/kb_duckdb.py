"""
Layer 5 — Knowledge base (DuckDB-backed).

This is the operator feedback loop. When someone confirms a root cause on the
dashboard, the incident's *signature* is written here; the next time a similar
incident appears, the platform recognises it and recommends the action that
worked last time. It is the difference between a tool that analyses history and
one that accumulates institutional knowledge.

Why DuckDB and not the Postgres stub in src/persistance/knowledge_base.py:
the entire platform runs on an embedded DuckDB file with no server. The stub's
*design* is right — incidents → ranked candidates → append-only operator
confirmations → an engine-accuracy view — so that design is ported here intact,
minus the server dependency. When the platform graduates to TimescaleDB, the
Postgres DDL is already written; the table shapes match.

## The incident signature

An operator doesn't confirm "incident INC-01268"; that id is meaningless next
week. What recurs is the *shape* of the failure:

    root-cause tag  +  the set of alarm types involved  +  the console/zone

So a signature is `(root_tag, sorted alarm-type set, zone)`. Two incidents with
the same signature are "the same kind of failure" even though their ids, exact
timestamps and peripheral alarms differ. That is what makes a confirmed case
reusable: match on signature, not on identity.

Matching is deliberately a plain, inspectable rule — exact root tag, Jaccard
overlap of alarm-type sets, same zone — not an embedding. An engineer can read
why a past case was suggested. When the pgvector retrieval lands (Tier 2), it
slots in behind the same `match()` interface without changing callers.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import duckdb
import pandas as pd


def signature_key(root_tag: str, alarm_types, zone: str | None) -> str:
    """Stable hash of (root tag, alarm-type set, zone). Order-independent on the
    types so the same failure hashes identically however its alarms were sorted."""
    types = ",".join(sorted(set(alarm_types)))
    raw = f"{root_tag}|{types}|{zone or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class KnowledgeBase:
    """Confirmed-incident store and recommender, embedded in the same DuckDB
    database as the alarm data."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self._init_schema()

    def _init_schema(self) -> None:
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS kb_confirmations (
                id VARCHAR PRIMARY KEY,
                signature VARCHAR,
                incident_id VARCHAR,
                root_tag VARCHAR,
                alarm_types VARCHAR,       -- JSON array, for display
                zone VARCHAR,
                confirmed_cause VARCHAR,   -- operator's cause (defaults to the engine hint)
                action_taken VARCHAR,      -- what fixed it — the reusable knowledge
                operator VARCHAR,
                engine_top_pick_correct BOOLEAN,
                confirmed_at TIMESTAMP
            );
        """)

    # -- write: the "Confirm root cause" button lands here -------------- #
    def confirm(self, *, incident_id: str, root_tag: str, alarm_types: list[str],
                zone: str | None, confirmed_cause: str, action_taken: str = "",
                operator: str = "operator", engine_top_pick_correct: bool = True
                ) -> dict:
        sig = signature_key(root_tag, alarm_types, zone)
        cid = hashlib.sha1(
            f"{incident_id}|{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]
        self.con.execute(
            "INSERT INTO kb_confirmations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [cid, sig, incident_id, root_tag, json.dumps(alarm_types), zone,
             confirmed_cause, action_taken, operator, engine_top_pick_correct,
             datetime.utcnow()])
        return {"confirmation_id": cid, "signature": sig,
                "prior_matches": self.count_signature(sig) - 1}

    def count_signature(self, sig: str) -> int:
        return int(self.con.execute(
            "SELECT count(*) FROM kb_confirmations WHERE signature = ?",
            [sig]).fetchone()[0])

    # -- read: match a live incident against confirmed history --------- #
    def match(self, root_tag: str, alarm_types: list[str], zone: str | None,
              min_overlap: float = 0.5) -> dict | None:
        """Return the best confirmed past case for this incident signature, or
        None. Exact root-tag match is required; alarm-type sets must overlap by
        `min_overlap` (Jaccard); same zone breaks ties. Plain and auditable."""
        rows = self.con.execute(
            "SELECT * FROM kb_confirmations WHERE root_tag = ? ORDER BY confirmed_at DESC",
            [root_tag]).df()
        if rows.empty:
            return None
        want = set(alarm_types)
        best, best_score = None, 0.0
        for r in rows.itertuples():
            have = set(json.loads(r.alarm_types))
            union = want | have
            jac = len(want & have) / len(union) if union else 0.0
            score = jac + (0.15 if r.zone == zone else 0.0)
            if jac >= min_overlap and score > best_score:
                best, best_score = r, score
        if best is None:
            return None
        return {
            "confirmed_cause": best.confirmed_cause,
            "action_taken": best.action_taken,
            "operator": best.operator,
            "confirmed_at": str(best.confirmed_at),
            "times_seen": self.count_signature(best.signature),
            "match_strength": round(best_score, 2),
        }

    def all_confirmations(self) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM kb_confirmations ORDER BY confirmed_at DESC").df()

    def engine_accuracy(self) -> dict:
        """How often the engine's #1 candidate matched what the operator
        confirmed — the metric that proves the loop works and justifies moving
        to Granger / vector methods later."""
        df = self.con.execute(
            "SELECT engine_top_pick_correct FROM kb_confirmations").df()
        if df.empty:
            return {"confirmations": 0, "top1_correct": 0, "accuracy": None}
        n = len(df)
        hits = int(df["engine_top_pick_correct"].sum())
        return {"confirmations": n, "top1_correct": hits,
                "accuracy": round(hits / n, 3)}

    def stats(self) -> dict:
        df = self.all_confirmations()
        return {
            "total": int(len(df)),
            "distinct_signatures": int(df["signature"].nunique()) if len(df) else 0,
            "recurring": int((df.groupby("signature").size() > 1).sum()) if len(df) else 0,
        }
