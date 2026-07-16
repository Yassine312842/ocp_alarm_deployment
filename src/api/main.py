"""
Layer 6 backend — FastAPI.

    uvicorn src.api.main:app --reload

Serves the analysis outputs as JSON for the React dashboard (and Grafana).
Every endpoint calls the same engine functions the CLI uses.

The analysis is cached. On the synthetic set it was cheap enough to recompute
per request; on the real log a cold pass is ~12s, and the dashboard fires eight
requests at once. The cache key is the store's row count + latest timestamp, so
a re-ingest invalidates it without a restart.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.analysis import engine, kpis
from src.persistance.kb_duckdb import KnowledgeBase
from src.rootcause import engine as rc
from src.rootcause import recommend as reco
from src.storage.duckdb_store import DuckStore

app = FastAPI(title="OCP Alarm Intelligence", version="0.3.0")


class ConfirmBody(BaseModel):
    incident_id: str
    confirmed_cause: str | None = None
    action_taken: str = ""
    operator: str = "operator"

# Dev CORS: the frontend may be served from a different origin (e.g. Vite on
# :5173). When FastAPI serves the built frontend itself, this is a no-op.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_CACHE: dict = {}
_STORE = {"db": None}
_KB_VERSION = {"v": 0}   # bumped on every confirmation so caches re-derive


def _store() -> DuckStore:
    if _STORE["db"] is None:
        _STORE["db"] = DuckStore()
    return _STORE["db"]


def _kb() -> KnowledgeBase:
    return KnowledgeBase(_store().con)


def _snapshot() -> dict:
    """Load + analyse once, reuse until the store (or the KB) changes."""
    store = _store()
    n = store.query("SELECT count(*) AS n, max(ts) AS m FROM alarms")
    key = (int(n["n"].iloc[0]), str(n["m"].iloc[0]), _KB_VERSION["v"])
    if _CACHE.get("key") == key:
        return _CACHE["snap"]

    alarms = store.alarms_df()
    episodes = store.episodes_df()
    if not alarms.empty:
        alarms["ts"] = pd.to_datetime(alarms["ts"])
    if not episodes.empty:
        episodes["start_ts"] = pd.to_datetime(episodes["start_ts"])

    ranked = rc.rank_candidates(alarms) if not alarms.empty else pd.DataFrame()
    snap = {
        "alarms": alarms,
        "episodes": episodes,
        "meta": store.meta(),
        "incidents": rc.incidents(alarms) if not alarms.empty else pd.DataFrame(),
        "ranked": ranked,
        "hints": rc.cause_hints(ranked) if len(ranked) else {},
    }
    _CACHE.update(key=key, snap=snap)
    return snap


def _alarms() -> pd.DataFrame:
    return _snapshot()["alarms"]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/summary")
def summary():
    snap = _snapshot()
    a = snap["alarms"]
    if a.empty:
        return {"total_alarms": 0, "by_priority": {}, "incidents": 0,
                "bad_actor_share": 0, "flood_windows": 0, "range": None,
                "dataset": None}
    active = a[a["state"] == "ACTIVE"]
    ba = engine.bad_actors(active, top=1)
    rate = kpis.alarm_rate(active)
    meta = snap["meta"]
    return {
        "total_alarms": int(len(active)),
        "by_priority": active["priority"].value_counts().to_dict(),
        "incidents": int(len(snap["incidents"])),
        "sources": int(active["tag"].nunique()),
        "bad_actor_share": float(ba["pct"].iloc[0]) if len(ba) else 0.0,
        "bad_actor_tag": str(ba["tag"].iloc[0]) if len(ba) else None,
        "flood_windows": int(rate.get("flood_windows", 0)),
        "range": [str(active["ts"].min()), str(active["ts"].max())],
        "dataset": meta.get("source", "synthetic generator"),
        "kind": meta.get("kind", "synthetic"),
    }


@app.get("/api/tags-status")
def tags_status(top: int = 60):
    """Per-tag annunciator state: alarm load + worst active priority."""
    a = _alarms()
    active = a[a["state"] == "ACTIVE"]
    if active.empty:
        return []
    order = engine.PRIORITY_ORDER
    rows = (active.assign(_o=active["priority"].map(order).fillna(0))
            .groupby("tag")
            .agg(alarm_count=("tag", "size"), _o=("_o", "max"),
                 last_ts=("ts", "max")).reset_index())
    inv = {v: k for k, v in order.items()}
    rows["worst_priority"] = rows["_o"].map(inv).fillna("LOW")
    rows["last_ts"] = rows["last_ts"].astype(str)
    rows = rows.sort_values("alarm_count", ascending=False).head(top)
    return rows[["tag", "alarm_count", "worst_priority", "last_ts"]].to_dict("records")


@app.get("/api/bad-actors")
def bad_actors(top: int = 10):
    return engine.bad_actors(_alarms(), top=top).to_dict("records")


@app.get("/api/chattering")
def chattering(top: int = 25):
    return engine.chattering(_alarms(), top=top).to_dict("records")


@app.get("/api/floods")
def floods():
    df = engine.alarm_floods(_alarms())
    if "window_start" in df:
        df["window_start"] = df["window_start"].astype(str)
    return df.to_dict("records")


@app.get("/api/co-occurrence")
def co_occurrence(top: int = 25):
    return engine.co_occurrence(_alarms(), top=top).to_dict("records")


@app.get("/api/sequences")
def sequences():
    return engine.pre_incident_sequences(_alarms()).to_dict("records")


@app.get("/api/kpis")
def kpi_summary():
    """EEMUA-191 scorecard: rate, priority mix, standing alarms, time to clear."""
    snap = _snapshot()
    if snap["alarms"].empty:
        return {}
    return kpis.summary_kpis(snap["alarms"], snap["episodes"])


@app.get("/api/incidents")
def incident_list(top: int = 40):
    """Incidents discovered in the alarm stream, biggest first."""
    inc = _snapshot()["incidents"]
    if inc.empty:
        return []
    out = inc.sort_values("alarm_count", ascending=False).head(top).copy()
    for c in ("start_ts", "end_ts"):
        out[c] = out[c].astype(str)
    return out.to_dict("records")


@app.get("/api/root-cause")
def root_cause(top: int = 40):
    """Ranked candidates for the biggest incidents (the ones worth triaging)."""
    snap = _snapshot()
    ranked, inc = snap["ranked"], snap["incidents"]
    if ranked.empty:
        return []
    keep = set(inc.sort_values("alarm_count", ascending=False)
               .head(top)["incident_id"])
    out = ranked[ranked["incident_id"].isin(keep)].copy()
    out["first_ts"] = out["first_ts"].astype(str)
    return out.to_dict("records")


@app.get("/api/cause-hints")
def cause_hints():
    """tag -> probable cause. Stands in for the Neo4j knowledge graph."""
    return _snapshot()["hints"]


# ------------------------------------------------------------------ #
# Knowledge base + recommendations
# ------------------------------------------------------------------ #
def _incident_signature(incident_id: str, snap: dict) -> dict | None:
    """Derive (root_tag, alarm_types, zone) for an incident from the ranked table."""
    ranked = snap["ranked"]
    rows = ranked[ranked["incident_id"] == incident_id]
    if rows.empty:
        return None
    rows = rows.sort_values("rank")
    top = rows.iloc[0]
    inc = snap["incidents"]
    zone = None
    if not inc.empty and "zone" in inc:
        z = inc.loc[inc["incident_id"] == incident_id, "zone"]
        zone = None if z.empty else z.iloc[0]
    return {"root_tag": top["tag"],
            "alarm_types": sorted(rows["level"].unique().tolist()),
            "zone": zone}


@app.get("/api/recommendations")
def recommendations(incident_id: str | None = None, top: int = 12):
    """Actionable next-steps. With ?incident_id=… returns that incident's
    recommendations; otherwise returns the biggest incidents' lead actions."""
    snap = _snapshot()
    ranked = snap["ranked"]
    if ranked.empty:
        return []
    kb = _kb()
    co = engine.co_occurrence(snap["alarms"], top=60) if not snap["alarms"].empty else pd.DataFrame()
    ch = engine.chattering(snap["alarms"], top=60) if not snap["alarms"].empty else pd.DataFrame()

    def one(iid: str) -> dict:
        sig = _incident_signature(iid, snap)
        kbm = kb.match(**sig) if sig else None
        recs = reco.recommend(iid, ranked, co_occurrence=co, chattering=ch, kb_match=kbm)
        return {"incident_id": iid, "signature": sig,
                "known_case": kbm is not None, "recommendations": recs}

    if incident_id:
        return one(incident_id)

    inc = snap["incidents"]
    ids = (inc.sort_values("alarm_count", ascending=False).head(top)["incident_id"].tolist()
           if not inc.empty else [])
    return [one(i) for i in ids]


@app.post("/api/confirm")
def confirm(body: ConfirmBody):
    """The 'Confirm root cause' button. Writes the incident's signature and the
    operator's confirmed cause/action to the knowledge base so future matching
    incidents are recognised."""
    snap = _snapshot()
    sig = _incident_signature(body.incident_id, snap)
    if sig is None:
        return {"error": "unknown incident"}
    ranked = snap["ranked"]
    top = ranked[ranked["incident_id"] == body.incident_id].sort_values("rank").iloc[0]
    cause = body.confirmed_cause or top.get("hint") or f"Investigate {sig['root_tag']}"
    res = _kb().confirm(
        incident_id=body.incident_id, root_tag=sig["root_tag"],
        alarm_types=sig["alarm_types"], zone=sig["zone"],
        confirmed_cause=cause, action_taken=body.action_taken,
        operator=body.operator, engine_top_pick_correct=True)
    _KB_VERSION["v"] += 1        # invalidate caches so the match shows up immediately
    return res


@app.get("/api/kb")
def kb_list(top: int = 100):
    """Confirmed incident history — the accumulated knowledge base."""
    df = _kb().all_confirmations().head(top)
    if df.empty:
        return []
    df["confirmed_at"] = df["confirmed_at"].astype(str)
    return df.to_dict("records")


@app.get("/api/kb-stats")
def kb_stats():
    kb = _kb()
    return {**kb.stats(), "engine_accuracy": kb.engine_accuracy()}


# Serve the frontend so `uvicorn src.api.main:app` runs the whole app at :8000.
_frontend = Path(__file__).resolve().parents[2] / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
