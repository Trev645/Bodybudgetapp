"""Comparable per-study KPIs — the common currency of benchmarking.

Every study is reduced to the same headline indicators, computed the same
way (normalise first, analyse second — spec Section 5 prerequisite).
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine, text

from awa.analysis import cascade, experience, metrics

KPI_FIELDS = [
    "average_utilisation_pct", "peak_occupancy_pct", "frequency_pct",
    "claimed_but_empty_pct", "attendance_rate", "sharing_ratio",
    "area_per_desk_m2", "experience_score",
]


def latest_study_id(engine: Engine, building_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT study_id FROM study WHERE building_id = :b "
            "AND status != 'open' ORDER BY end_date DESC LIMIT 1"),
            {"b": building_id}).fetchone()
    return row.study_id if row else None


def study_kpis(engine: Engine, study_id: str) -> dict | None:
    obs = metrics.load_observations(engine, study_id)
    if obs.empty:
        return None
    desk = metrics.utilisation_summary(obs, "desk")
    casc = cascade.headcount_cascade(engine, study_id)
    exp = experience.experience_summary(engine, study_id)
    with engine.connect() as conn:
        area = conn.execute(text(
            "SELECT b.gross_area_m2, "
            "(SELECT COUNT(*) FROM setting s WHERE s.building_id = b.building_id "
            " AND s.type = 'desk') AS desks "
            "FROM building b JOIN study st ON st.building_id = b.building_id "
            "WHERE st.study_id = :s"), {"s": study_id}).fetchone()
    area_per_desk = round(area.gross_area_m2 / area.desks, 1) \
        if area and area.gross_area_m2 and area.desks else None
    return {
        "average_utilisation_pct": desk.get("average_utilisation_pct"),
        "peak_occupancy_pct": desk.get("peak_occupancy_pct"),
        "frequency_pct": desk.get("frequency_pct"),
        "claimed_but_empty_pct": desk.get("claimed_but_empty_pct"),
        "attendance_rate": casc.get("attendance_rate"),
        "sharing_ratio": casc.get("sharing_ratio"),
        "area_per_desk_m2": area_per_desk,
        "experience_score": exp.get("overall_score"),
        "assigned_headcount": casc.get("assigned_population"),
        "desks_provided": casc.get("desks_provided"),
    }


def kpi_percentiles(rows: list[dict]) -> dict:
    """p25 / median / p75 across peer studies for each KPI."""
    df = pd.DataFrame(rows)
    out = {}
    for field in KPI_FIELDS:
        if field not in df.columns:
            continue
        series = pd.to_numeric(df[field], errors="coerce").dropna()
        if series.empty:
            continue
        out[field] = {
            "p25": round(float(series.quantile(0.25)), 2),
            "median": round(float(series.quantile(0.50)), 2),
            "p75": round(float(series.quantile(0.75)), 2),
            "n": int(len(series)),
        }
    return out
