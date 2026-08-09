"""Desk-not-found risk (spec 4.2) and percentile desk sizing (spec 4.3).

The flagship calculation: the acceptable failure rate is an INPUT
parameter; the required desk count at that percentile of observed team
demand is the OUTPUT. A 5% acceptable failure rate reads the 95th
percentile of the demand distribution.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text


def team_demand_by_round(engine: Engine, study_id: str) -> pd.DataFrame:
    """Desks needed per team per round, measured from observations.

    Demand = desks occupied by members of the team in that round. Uses the
    observed occupant team where recorded, falling back to the desk's
    allocated team. Rounds where a team needed no desks count as zero —
    the distribution must include quiet rounds or percentiles overstate.
    """
    df = pd.read_sql(text("""
        SELECT r.round_id, r.label AS round_label,
               COALESCE(o.occupant_team_id, s.allocated_team_id) AS team_id,
               SUM(o.occupied) AS demand
        FROM observation o
        JOIN setting s ON s.setting_id = o.setting_id
        JOIN round r ON r.round_id = o.round_id
        WHERE o.study_id = :sid AND s.type = 'desk'
        GROUP BY r.round_id, r.label, COALESCE(o.occupant_team_id,
                                               s.allocated_team_id)
        """), engine, params={"sid": study_id})
    df = df.dropna(subset=["team_id"])
    if df.empty:
        return df
    # Densify: every team appears in every round, zero-filled.
    rounds = df[["round_id", "round_label"]].drop_duplicates()
    teams = df[["team_id"]].drop_duplicates()
    full = rounds.merge(teams, how="cross")
    df = full.merge(df, on=["round_id", "round_label", "team_id"], how="left")
    df["demand"] = df["demand"].fillna(0).astype(int)
    return df


def _teams(engine: Engine, study_id: str) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT t.team_id, t.name, t.allocated_desks,
               (SELECT COUNT(*) FROM person_assignment p
                WHERE p.team_id = t.team_id) AS assigned_headcount
        FROM team t
        WHERE t.building_id = (SELECT building_id FROM study
                               WHERE study_id = :sid)
        """), engine, params={"sid": study_id})


def desk_not_found_risk(engine: Engine, study_id: str) -> list[dict]:
    """Measured failure rate per team: rounds where demand > supply."""
    demand = team_demand_by_round(engine, study_id)
    teams = _teams(engine, study_id)
    out = []
    for team in teams.itertuples():
        d = demand[demand["team_id"] == team.team_id]["demand"]
        if d.empty or team.allocated_desks <= 0:
            continue
        fails = int((d > team.allocated_desks).sum())
        out.append({
            "team": team.name,
            "allocated_desks": int(team.allocated_desks),
            "rounds": int(len(d)),
            "fail_rounds": fails,
            "failure_rate_pct": round(100 * fails / len(d), 1),
            "peak_demand": int(d.max()),
            "mean_demand": round(float(d.mean()), 1),
        })
    return sorted(out, key=lambda r: -r["failure_rate_pct"])


def desks_required(engine: Engine, study_id: str,
                   acceptable_failure_rate_pct: float = 5.0) -> dict:
    """Desk count needed per team (and overall) at a chosen failure rate.

    percentile = 100 - acceptable failure rate; 'higher' interpolation so
    the returned count always actually meets the target on observed data.
    """
    if not 0 < acceptable_failure_rate_pct < 100:
        raise ValueError("acceptable_failure_rate_pct must be between 0 and 100")
    pct = 100 - acceptable_failure_rate_pct
    demand = team_demand_by_round(engine, study_id)
    teams = _teams(engine, study_id)
    per_team, total_required = [], 0
    for team in teams.itertuples():
        d = demand[demand["team_id"] == team.team_id]["demand"]
        if d.empty:
            continue
        required = int(np.percentile(d, pct, method="higher"))
        total_required += required
        per_team.append({
            "team": team.name,
            "assigned_headcount": int(team.assigned_headcount),
            "allocated_desks": int(team.allocated_desks),
            "required_desks": required,
            "delta_vs_allocated": required - int(team.allocated_desks),
            "sharing_ratio": round(team.assigned_headcount / required, 2)
            if required else None,
        })
    # Building level: total desk demand per round pooled across teams —
    # sharing across teams means the building peak < sum of team peaks.
    building = demand.groupby("round_id")["demand"].sum() if not demand.empty \
        else pd.Series(dtype=int)
    building_required = int(np.percentile(building, pct, method="higher")) \
        if not building.empty else 0
    assigned = int(teams["assigned_headcount"].sum())
    return {
        "acceptable_failure_rate_pct": acceptable_failure_rate_pct,
        "demand_percentile": pct,
        "per_team": per_team,
        "building": {
            "assigned_headcount": assigned,
            "required_desks_pooled": building_required,
            "required_desks_team_by_team": total_required,
            "pooling_saving_desks": total_required - building_required,
            "sharing_ratio_pooled": round(assigned / building_required, 2)
            if building_required else None,
        },
    }
