"""
Layer 5 — Recommendations: "what should the operator check next?"

Root-cause analysis says *which tag* is probably to blame. That is not yet
actionable — an operator still has to decide what to *do*. This module turns a
ranked incident into concrete, prioritised next steps, drawing on three sources
in order of authority:

  1. **Confirmed history (knowledge base).** If this incident's signature matches
     a case an operator already diagnosed and fixed, that is the strongest
     possible recommendation: "seen 3× before — last resolved by <action>." This
     is the payoff of the feedback loop.

  2. **Alarm-type playbook.** Absent a confirmed match, each alarm type has a
     standard first response (an OFFLINE alarm → check the gateway/BACnet trunk;
     a chattering instrument → widen the deadband). Rule-based, transparent, and
     the seed the knowledge base grows from.

  3. **Incident structure.** Signals from the incident itself: the top
     co-firing tags to inspect together, whether the lead tag is chattering
     (instrument problem, not process), whether a standing alarm is masking the
     picture.

Every recommendation carries *why*, so the operator can judge it rather than
obey it. The output is ordered by priority so the console shows the single most
useful action first.
"""
from __future__ import annotations

import pandas as pd

from src.acquisition.alarm_log import TYPE_CAUSE_HINT

# First-response playbook per alarm type. Kept short and imperative — this is
# what an operator reads at 3am, not a manual.
TYPE_ACTION: dict[str, str] = {
    "OFFLINE":          "Confirm the controller/gateway has power and its BACnet trunk is up; ping the device before assuming a field fault.",
    "COMMS_FAULT":      "Check the field-bus segment and device address; a single noisy device can take down the trunk.",
    "SPACE_TEMP":       "Verify the VAV damper travels and reheat responds; confirm upstream AHU is delivering air.",
    "SETPOINT":         "Check for an active schedule change, occupancy override, or a setpoint left in manual.",
    "TEMP_HIGH":        "Trace the chilled-water loop: supply temperature, valve command vs. position, coil ΔT.",
    "TEMP_LOW":         "Check the heating/cooling valve isn't stuck open and the controller output matches demand.",
    "AIR_TEMP":         "Inspect the AHU/MAU coil, valve and discharge sensor against setpoint.",
    "FAN":              "Confirm fan command vs. status and airflow proving; suspect the starter/VFD or a stuck damper.",
    "CHILLER":          "Review the chiller start/stop sequence and any lockout; check condenser and flow.",
    "GAS":              "Treat as a safety event first: verify detector reading, follow the gas-response procedure, then investigate cause.",
    "WATER_LEAK":       "Isolate the affected zone and inspect physically before clearing.",
    "FIRE_SUPERVISORY": "Check the fire panel for the supervisory/trouble source; do not silence without confirming.",
    "SECURITY":         "Review the access-control event; correlate with door/gate status.",
    "TRIP":             "Confirm the equipment trip is real, check interlocks and the trip cause before any restart.",
    "MANUAL_MODE":      "A point is in manual/override — its alarm is suppressed at source. Return it to auto once safe.",
    "METER":            "Check the meter/consumption channel for a fault or a genuine threshold breach.",
    "POWER":            "Check the power/battery system and any UPS status.",
    "GENERAL":          "Review the point configuration and recent changes; the alarm is uncategorised.",
}


def recommend(incident_id: str, ranked: pd.DataFrame, *,
              co_occurrence: pd.DataFrame | None = None,
              chattering: pd.DataFrame | None = None,
              standing: pd.DataFrame | None = None,
              kb_match: dict | None = None) -> list[dict]:
    """Build an ordered list of recommendations for one incident.

    `ranked` is the incident's slice of the root-cause table (already ranked).
    The optional frames add structural signals. `kb_match` is the result of
    KnowledgeBase.match() for this incident's signature, if any.

    Each recommendation: {priority, action, why, tags, source}.
    """
    rows = ranked[ranked["incident_id"] == incident_id].sort_values("rank")
    if rows.empty:
        return []
    top = rows.iloc[0]
    recs: list[dict] = []

    # 1 — confirmed history wins outright
    if kb_match:
        seen = kb_match.get("times_seen", 1)
        act = kb_match.get("action_taken") or kb_match.get("confirmed_cause")
        recs.append({
            "priority": 1,
            "action": act or f"Apply the previously confirmed fix for {top.tag}.",
            "why": (f"This failure signature has been confirmed {seen}× before"
                    + (f"; last resolved by {kb_match['operator']} "
                       f"on {kb_match['confirmed_at'][:10]}." if kb_match.get('operator') else ".")),
            "tags": [top.tag],
            "source": "knowledge base",
        })

    # 2 — playbook for the lead candidate's alarm type
    play = TYPE_ACTION.get(top.level, TYPE_ACTION["GENERAL"])
    recs.append({
        "priority": 1 if not kb_match else 2,
        "action": f"Inspect {top.tag} first — {play}",
        "why": (getattr(top, "explanation", "") or
                TYPE_CAUSE_HINT.get(top.level, "highest-ranked candidate")),
        "tags": [top.tag],
        "source": "root-cause + playbook",
    })

    # 3 — is the lead tag chattering? Then it's an instrument problem
    if chattering is not None and not chattering.empty:
        ch = chattering[chattering["tag"] == top.tag]
        if not ch.empty:
            pct = ch.iloc[0].get("chatter_pct", 0)
            recs.append({
                "priority": 2,
                "action": f"Widen the deadband / add on-delay on {top.tag} before chasing a process cause.",
                "why": (f"{top.tag} chatters ({pct}% of its activations repeat within 60 s) — "
                        "the alarm load here is an instrument-tuning problem, not a plant upset."),
                "tags": [top.tag],
                "source": "chattering analysis",
            })

    # 4 — the tags to inspect alongside the lead (co-firing partners)
    if co_occurrence is not None and not co_occurrence.empty:
        partners = co_occurrence[(co_occurrence["tag_a"] == top.tag) |
                                 (co_occurrence["tag_b"] == top.tag)]
        partner_tags = []
        for r in partners.head(3).itertuples():
            partner_tags.append(r.tag_b if r.tag_a == top.tag else r.tag_a)
        if partner_tags:
            recs.append({
                "priority": 3,
                "action": f"Check {', '.join(partner_tags[:2])} in the same sweep — they alarm together with {top.tag}.",
                "why": "These sources co-fire with the lead candidate, so they likely share a cause or a common feed.",
                "tags": partner_tags,
                "source": "co-occurrence",
            })

    # 5 — is a standing alarm masking the picture in this zone?
    if standing is not None and not standing.empty and "zone" in getattr(top, "_fields", ()):
        z = getattr(top, "zone", None)
        if z is not None:
            sa = standing[standing.get("zone", pd.Series(dtype=str)) == z] if "zone" in standing else pd.DataFrame()
            if not sa.empty:
                recs.append({
                    "priority": 3,
                    "action": f"Clear or rationalise the standing alarm(s) in {z} — they mask new events.",
                    "why": "A permanently-active alarm in this console trains operators to ignore the annunciator.",
                    "tags": list(sa["tag"].head(2)),
                    "source": "standing alarms",
                })

    # de-dup by action, keep the highest priority (lowest number) first
    seen_actions = set()
    out = []
    for r in sorted(recs, key=lambda r: r["priority"]):
        if r["action"] not in seen_actions:
            seen_actions.add(r["action"])
            out.append(r)
    return out
