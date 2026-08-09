"""The headcount cascade (spec 4.4).

Assigned population × attendance rate × utilisation = desk demand.
Every factor is measured from the data, not assumed. The sharing ratio
lives mostly in the attendance step — it is a bet on people not all
showing up at once.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine, text


def headcount_cascade(engine: Engine, study_id: str) -> dict:
    people = pd.read_sql(text("""
        SELECT employment_type, COUNT(*) AS n FROM person_assignment
        WHERE building_id = (SELECT building_id FROM study WHERE study_id = :sid)
        GROUP BY employment_type
        """), engine, params={"sid": study_id})
    assigned = int(people["n"].sum())

    # People present in the building per round: occupied desks plus counted
    # meeting-room occupants (a measured lower bound on presence — people in
    # transit, at lunch or in uncounted rooms are invisible to observation).
    presence = pd.read_sql(text("""
        SELECT r.round_id, DATE(r.ts) AS day,
               SUM(CASE WHEN s.type = 'desk' THEN o.occupied ELSE 0 END)
                   AS desks_occupied,
               SUM(CASE WHEN s.type = 'meeting_room' AND o.occupied = 1
                        THEN COALESCE(o.occupant_count, 0) ELSE 0 END)
                   AS room_occupants
        FROM observation o
        JOIN setting s ON s.setting_id = o.setting_id
        JOIN round r ON r.round_id = o.round_id
        WHERE o.study_id = :sid
        GROUP BY r.round_id, DATE(r.ts)
        """), engine, params={"sid": study_id})
    desks_provided = pd.read_sql(text("""
        SELECT COUNT(*) AS n FROM setting
        WHERE building_id = (SELECT building_id FROM study WHERE study_id = :sid)
          AND type = 'desk'
        """), engine, params={"sid": study_id})["n"].iloc[0]

    if presence.empty or assigned == 0:
        return {"note": "cascade needs both observations and a headcount list",
                "assigned_population": assigned}

    presence["present"] = presence["desks_occupied"] + presence["room_occupants"]
    daily_peak_present = presence.groupby("day")["present"].max()
    avg_present = float(daily_peak_present.mean())
    attendance_rate = avg_present / assigned

    avg_desks_occupied = float(presence["desks_occupied"].mean())
    utilisation_factor = avg_desks_occupied / avg_present if avg_present else 0.0

    return {
        "assigned_population": assigned,
        "assigned_split": {r.employment_type: int(r.n)
                           for r in people.itertuples()},
        "attendance_rate": round(attendance_rate, 3),
        "avg_daily_peak_present": round(avg_present, 1),
        "utilisation_factor": round(utilisation_factor, 3),
        "avg_desk_demand": round(avg_desks_occupied, 1),
        "peak_desk_demand": int(presence["desks_occupied"].max()),
        "desks_provided": int(desks_provided),
        "sharing_ratio": round(assigned / desks_provided, 2)
        if desks_provided else None,
        "note": ("presence is measured as occupied desks + counted room "
                 "occupants: a lower bound; attendance uses daily peaks"),
    }
