"""Research engine over the pooled, anonymised corpus (spec Section 5).

The priority research question: what is the relationship between the
people assigned to a building, its relative capacity, and the perceived
user experience? Each study becomes one anonymous data point —
capacity ratio (assigned headcount ÷ desks provided), measured
utilisation, and mean experience score — carrying only sector, size band
and region as context. Admin-only: raw rows never reach client tenants.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from awa.analysis.experience import find_tipping_point
from awa.benchmarking.kpis import study_kpis


def corpus_points(engine: Engine) -> pd.DataFrame:
    """One anonymised row per completed study across all clients."""
    studies = pd.read_sql(text("""
        SELECT st.study_id, c.sector, b.size_band, b.region
        FROM study st
        JOIN building b ON b.building_id = st.building_id
        JOIN client c ON c.client_id = st.client_id
        WHERE st.status != 'open'
        """), engine)
    rows = []
    for s in studies.itertuples():
        kpis = study_kpis(engine, s.study_id)
        if not kpis:
            continue
        assigned = kpis.get("assigned_headcount") or 0
        desks = kpis.get("desks_provided") or 0
        rows.append({
            "sector": s.sector, "size_band": s.size_band, "region": s.region,
            "capacity_ratio": round(assigned / desks, 3) if desks else None,
            "average_utilisation_pct": kpis.get("average_utilisation_pct"),
            "peak_occupancy_pct": kpis.get("peak_occupancy_pct"),
            "attendance_rate": kpis.get("attendance_rate"),
            "experience_score": kpis.get("experience_score"),
        })
    return pd.DataFrame(rows)


def _correlation(df: pd.DataFrame, x: str, y: str) -> dict | None:
    sub = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(sub) < 3 or sub[x].std() == 0 or sub[y].std() == 0:
        return None
    return {"r": round(float(np.corrcoef(sub[x], sub[y])[0, 1]), 3),
            "n": int(len(sub))}


def capacity_experience_research(engine: Engine) -> dict:
    """Corpus-wide answers to the priority research questions."""
    df = corpus_points(engine)
    if df.empty:
        return {"studies": 0, "note": "no completed studies in the corpus yet"}

    result: dict = {"studies": int(len(df))}

    # Q1: is there a universal tipping point? Utilisation level beyond which
    # experience collapses, across all companies.
    sub = df[["average_utilisation_pct", "experience_score"]].dropna()
    tipping = find_tipping_point(sub["average_utilisation_pct"].to_numpy(),
                                 sub["experience_score"].to_numpy()) \
        if not sub.empty else None
    result["universal_tipping_point"] = tipping or "insufficient data points"

    # Core relationship: assigned people vs relative capacity vs experience.
    result["correlations"] = {
        "capacity_ratio_vs_experience":
            _correlation(df, "capacity_ratio", "experience_score"),
        "utilisation_vs_experience":
            _correlation(df, "average_utilisation_pct", "experience_score"),
        "attendance_vs_experience":
            _correlation(df, "attendance_rate", "experience_score"),
        "capacity_ratio_vs_utilisation":
            _correlation(df, "capacity_ratio", "average_utilisation_pct"),
    }

    # Q2: does the relationship flex by context (sector)?
    by_sector = {}
    for sector, g in df.groupby("sector"):
        if len(g) < 3:
            by_sector[sector] = {"n": int(len(g)),
                                 "note": "too few studies to characterise"}
            continue
        by_sector[sector] = {
            "n": int(len(g)),
            "mean_capacity_ratio": round(
                float(pd.to_numeric(g["capacity_ratio"]).mean()), 2),
            "mean_utilisation_pct": round(
                float(pd.to_numeric(g["average_utilisation_pct"]).mean()), 1),
            "mean_experience_score": round(
                float(pd.to_numeric(g["experience_score"]).dropna().mean()), 2)
            if g["experience_score"].notna().any() else None,
            "utilisation_vs_experience": _correlation(
                g, "average_utilisation_pct", "experience_score"),
        }
    result["by_sector"] = by_sector

    result["notes"] = [
        "each point is one anonymised study (sector/size_band/region only)",
        "cognitive-performance linkage requires a cognitive load instrument "
        "in the survey battery — add items and they flow through automatically",
    ]
    return result
