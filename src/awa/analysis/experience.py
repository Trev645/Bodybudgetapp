"""Experience overlay (spec 4.5) and tipping-point detection.

Experience needs its own evidence. The overlay plots perceived experience
(survey scores) against measured utilisation pressure per team, then
looks for the utilisation level beyond which satisfaction falls off a
cliff — the number that sets the right sharing ratio.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from awa.analysis.sizing import team_demand_by_round

MIN_POINTS_FOR_TIPPING = 6


def experience_summary(engine: Engine, study_id: str) -> dict:
    df = pd.read_sql(text("""
        SELECT item, score, team_id FROM experience_survey
        WHERE study_id = :sid
        """), engine, params={"sid": study_id})
    if df.empty:
        return {"responses": 0, "note": "no survey data ingested for this study"}
    return {
        "responses": int(df[["team_id"]].assign(n=1)["n"].count()),
        "overall_score": round(float(df["score"].mean()), 2),
        "by_item": {i: round(float(g["score"].mean()), 2)
                    for i, g in df.groupby("item")},
    }


def team_overlay_points(engine: Engine, study_id: str) -> pd.DataFrame:
    """One point per team: utilisation pressure vs mean experience score."""
    demand = team_demand_by_round(engine, study_id)
    survey = pd.read_sql(text("""
        SELECT team_id, AVG(score) AS experience_score,
               COUNT(DISTINCT response_id) AS responses
        FROM experience_survey
        WHERE study_id = :sid AND team_id IS NOT NULL
        GROUP BY team_id
        """), engine, params={"sid": study_id})
    teams = pd.read_sql(text("""
        SELECT t.team_id, t.name, t.allocated_desks,
               (SELECT COUNT(*) FROM person_assignment p
                WHERE p.team_id = t.team_id) AS assigned_headcount
        FROM team t
        WHERE t.building_id = (SELECT building_id FROM study
                               WHERE study_id = :sid)
        """), engine, params={"sid": study_id})
    if demand.empty or survey.empty:
        return pd.DataFrame()
    util = demand.groupby("team_id")["demand"].agg(["mean", "max"]).reset_index()
    util.columns = ["team_id", "mean_demand", "peak_demand"]
    df = teams.merge(util, on="team_id").merge(survey, on="team_id")
    df = df[df["allocated_desks"] > 0]
    df["utilisation_pct"] = 100 * df["mean_demand"] / df["allocated_desks"]
    df["peak_utilisation_pct"] = 100 * df["peak_demand"] / df["allocated_desks"]
    df["capacity_ratio"] = df["assigned_headcount"] / df["allocated_desks"]
    return df


def find_tipping_point(x: np.ndarray, y: np.ndarray) -> dict | None:
    """Two-segment piecewise-linear fit: the breakpoint minimising total SSE.

    Reported only when the post-break slope is materially steeper downward
    than the pre-break slope — i.e. experience genuinely falls off a cliff
    rather than declining smoothly.
    """
    if len(x) < MIN_POINTS_FOR_TIPPING:
        return None
    order = np.argsort(x)
    x, y = x[order].astype(float), y[order].astype(float)

    def sse(xs, ys):
        if len(xs) < 2 or np.ptp(xs) == 0:
            return float(((ys - ys.mean()) ** 2).sum()), 0.0
        slope, intercept = np.polyfit(xs, ys, 1)
        return float(((ys - (slope * xs + intercept)) ** 2).sum()), float(slope)

    flat_sse, overall_slope = sse(x, y)
    best = None
    for i in range(2, len(x) - 2):
        left_sse, left_slope = sse(x[:i], y[:i])
        right_sse, right_slope = sse(x[i:], y[i:])
        total = left_sse + right_sse
        if best is None or total < best["sse"]:
            best = {"sse": total, "breakpoint": float(x[i]),
                    "slope_before": round(left_slope, 4),
                    "slope_after": round(right_slope, 4)}
    if best is None:
        return None
    improved = best["sse"] < 0.8 * flat_sse
    cliff = best["slope_after"] < min(0.0, best["slope_before"])
    return {
        "tipping_point_x": round(best["breakpoint"], 1),
        "slope_before": best["slope_before"],
        "slope_after": best["slope_after"],
        "confident": bool(improved and cliff),
        "n_points": int(len(x)),
        "overall_slope": round(overall_slope, 4),
    }


def experience_overlay(engine: Engine, study_id: str) -> dict:
    points = team_overlay_points(engine, study_id)
    if points.empty:
        return {"note": "overlay needs both observations and survey data "
                        "joined to teams"}
    tipping = find_tipping_point(points["utilisation_pct"].to_numpy(),
                                 points["experience_score"].to_numpy())
    corr = None
    if len(points) >= 3 and points["utilisation_pct"].std() > 0 \
            and points["experience_score"].std() > 0:
        corr = round(float(np.corrcoef(points["utilisation_pct"],
                                       points["experience_score"])[0, 1]), 3)
    return {
        "points": points[["name", "utilisation_pct", "peak_utilisation_pct",
                          "capacity_ratio", "experience_score", "responses"]]
        .round(2).rename(columns={"name": "team"}).to_dict("records"),
        "correlation_utilisation_vs_experience": corr,
        "tipping_point": tipping or
        f"needs at least {MIN_POINTS_FOR_TIPPING} team data points",
    }
