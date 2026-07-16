"""
Knowledge-base persistence layer for OCP Alarm Intelligence.

Replaces the "Confirm root cause" stub with real writes against Postgres/
TimescaleDB (see sql/knowledge_base_schema.sql for the DDL).

Usage from your FastAPI route, e.g. src/api/routes/incidents.py:

    from persistence.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(dsn=settings.DATABASE_URL)

    @router.post("/incidents/{incident_id}/confirm-root-cause")
    def confirm_root_cause(incident_id: str, body: ConfirmRootCauseRequest):
        return kb.confirm_root_cause(
            incident_id=incident_id,
            confirmed_tag=body.confirmed_tag,
            operator_id=body.operator_id,
            candidate_id=body.candidate_id,
            free_text_note=body.note,
        )

Adjust the connection details / ORM choice to match whatever the rest of
src/ already uses (this uses plain psycopg2 + context managers to avoid
assuming you're on SQLAlchemy — swap in your existing DB session pattern
if you have one).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


@dataclass
class RootCauseCandidate:
    id: str
    tag: str
    method: str
    rank: int
    confidence: Optional[float]
    explanation: Optional[str]


class KnowledgeBase:
    def __init__(self, dsn: str):
        """dsn: e.g. 'postgresql://user:pass@localhost:5432/alarm_intel'"""
        self._dsn = dsn

    @contextmanager
    def _conn(self):
        conn = psycopg2.connect(self._dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Incident + candidate creation (called by the RCA engine, not the UI)
    # ------------------------------------------------------------------
    def create_incident(
        self,
        started_at: datetime,
        alarms: list[dict],
        candidates: list[RootCauseCandidate],
        unit: Optional[str] = None,
    ) -> str:
        """Persist a newly-detected incident plus the engine's ranked candidates.

        alarms: list of dicts with keys tag, description, priority, occurred_at,
                operator_id, sequence_rank
        """
        incident_id = str(uuid.uuid4())
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (id, started_at, unit, alarm_count, status)
                VALUES (%s, %s, %s, %s, 'open')
                """,
                (incident_id, started_at, unit, len(alarms)),
            )

            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO incident_alarms
                    (incident_id, tag, description, priority, occurred_at, operator_id, sequence_rank)
                VALUES %s
                """,
                [
                    (
                        incident_id,
                        a["tag"],
                        a.get("description"),
                        a.get("priority"),
                        a["occurred_at"],
                        a.get("operator_id"),
                        a.get("sequence_rank"),
                    )
                    for a in alarms
                ],
            )

            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO root_cause_candidates
                    (incident_id, tag, method, rank, confidence, explanation)
                VALUES %s
                """,
                [
                    (
                        incident_id,
                        c.tag,
                        c.method,
                        c.rank,
                        c.confidence,
                        c.explanation,
                    )
                    for c in candidates
                ],
            )

        logger.info("Created incident %s with %d candidates", incident_id, len(candidates))
        return incident_id

    # ------------------------------------------------------------------
    # This is the piece that replaces the stub.
    # ------------------------------------------------------------------
    def confirm_root_cause(
        self,
        incident_id: str,
        confirmed_tag: str,
        operator_id: str,
        candidate_id: Optional[str] = None,
        free_text_note: Optional[str] = None,
    ) -> dict:
        """Record an operator's confirmed root cause for an incident.

        Determines whether the engine's #1-ranked candidate was correct
        (for the engine_accuracy view / future model evaluation), then
        writes an append-only confirmation row.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, tag FROM root_cause_candidates
                WHERE incident_id = %s AND rank = 1
                """,
                (incident_id,),
            )
            top_candidate = cur.fetchone()
            top_pick_correct = (
                top_candidate is not None and top_candidate["tag"] == confirmed_tag
            )

            confirmation_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO operator_confirmations
                    (id, incident_id, candidate_id, confirmed_tag, free_text_note,
                     operator_id, engine_top_pick_correct)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, confirmed_at
                """,
                (
                    confirmation_id,
                    incident_id,
                    candidate_id,
                    confirmed_tag,
                    free_text_note,
                    operator_id,
                    top_pick_correct,
                ),
            )
            row = cur.fetchone()

        logger.info(
            "Incident %s confirmed by %s: tag=%s top_pick_correct=%s",
            incident_id, operator_id, confirmed_tag, top_pick_correct,
        )
        return {
            "confirmation_id": row["id"],
            "confirmed_at": row["confirmed_at"],
            "engine_top_pick_correct": top_pick_correct,
        }

    # ------------------------------------------------------------------
    # Reads used by the frontend / KPI layer
    # ------------------------------------------------------------------
    def get_engine_accuracy(self, days: int = 30) -> list[dict]:
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM engine_accuracy
                WHERE day >= now() - (%s || ' days')::interval
                ORDER BY day DESC
                """,
                (days,),
            )
            return cur.fetchall()

    def get_incident(self, incident_id: str) -> Optional[dict]:
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            incident = cur.fetchone()
            if not incident:
                return None
            cur.execute(
                "SELECT * FROM incident_alarms WHERE incident_id = %s ORDER BY occurred_at",
                (incident_id,),
            )
            incident["alarms"] = cur.fetchall()
            cur.execute(
                "SELECT * FROM root_cause_candidates WHERE incident_id = %s ORDER BY rank",
                (incident_id,),
            )
            incident["candidates"] = cur.fetchall()
            cur.execute(
                "SELECT * FROM operator_confirmations WHERE incident_id = %s ORDER BY confirmed_at",
                (incident_id,),
            )
            incident["confirmations"] = cur.fetchall()
            return incident
