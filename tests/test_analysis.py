"""Metrics validated against a hand-checked sample (spec 7.3 step 3)."""

import numpy as np
import pandas as pd
import pytest

from awa import ingestion
from awa.analysis import metrics, sizing
from awa.analysis.experience import find_tipping_point
from awa.security import create_client, new_id, size_band_for_area
from sqlalchemy import text
from datetime import datetime, timezone


def make_building(engine, client_id, area=10_000):
    building_id = new_id("bld")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO building (building_id, client_id, name, gross_area_m2, "
            "region, size_band, created_at) VALUES (:i, :c, 'HQ', :a, 'london', "
            ":s, :t)"),
            {"i": building_id, "c": client_id, "a": area,
             "s": size_band_for_area(area),
             "t": datetime.now(timezone.utc).isoformat()})
    return building_id


@pytest.fixture()
def small_study(engine):
    """4 desks, one team, 2 rounds — every number hand-checkable.

    Round 1: D1 occupied, D2 occupied, D3 claimed, D4 empty -> 50% occupied
    Round 2: D1 occupied, D2 occupied, D3 occupied, D4 empty -> 75% occupied
    """
    client_id, _ = create_client(engine, name="T", sector="tech",
                                 size_band="medium", region="london")
    building_id = make_building(engine, client_id)
    ingestion.ingest_teams(engine, client_id, building_id,
                           pd.DataFrame([{"team": "Alpha",
                                          "allocated_desks": 4}]))
    ingestion.ingest_settings(engine, client_id, building_id, pd.DataFrame([
        {"setting_code": f"D{i}", "type": "desk", "floor": "1",
         "team": "Alpha"} for i in range(1, 5)]))
    ingestion.ingest_headcount(engine, client_id, building_id, pd.DataFrame([
        {"person_ref": f"P{i}", "team": "Alpha", "employment_type": "employee"}
        for i in range(6)]))
    study_id = ingestion.create_study(engine, client_id, building_id,
                                      start_date="2026-03-02",
                                      end_date="2026-03-03", interval_mins=60)
    rows = []
    for rnd, ts, statuses in [
        ("R1", "2026-03-02T10:00", ["occupied", "occupied", "claimed", "empty"]),
        ("R2", "2026-03-02T14:00", ["occupied", "occupied", "occupied", "empty"]),
    ]:
        for i, status in enumerate(statuses, start=1):
            rows.append({"setting_code": f"D{i}", "ts": ts, "round": rnd,
                         "status": status,
                         "team": "Alpha" if status == "occupied" else None})
    report = ingestion.ingest_observations(engine, client_id, study_id,
                                           pd.DataFrame(rows))
    assert report.ok and report.rows_ingested == 8
    return engine, study_id


def test_hand_checked_utilisation(small_study):
    engine, study_id = small_study
    obs = metrics.load_observations(engine, study_id)
    summary = metrics.utilisation_summary(obs, "desk")
    assert summary["peak_occupancy_pct"] == 75.0
    assert summary["average_utilisation_pct"] == 62.5   # (50 + 75) / 2
    # D1, D2 occupied both rounds; D3 used both rounds (claimed then
    # occupied) but occupied only once; D4 never used.
    assert summary["frequency_pct"] == 75.0             # (100+100+100+0)/4
    assert summary["occupancy_pct"] == 62.5             # (100+100+50+0)/4
    assert summary["claimed_but_empty_pct"] == 12.5


def test_desk_not_found_and_sizing(small_study):
    engine, study_id = small_study
    # Demand: R1 = 2, R2 = 3 against 4 allocated desks -> no fail rounds.
    risk = sizing.desk_not_found_risk(engine, study_id)
    assert risk[0]["failure_rate_pct"] == 0.0
    assert risk[0]["peak_demand"] == 3
    result = sizing.desks_required(engine, study_id, 5.0)
    team = result["per_team"][0]
    # 95th percentile (higher) of [2, 3] = 3 desks.
    assert team["required_desks"] == 3
    assert team["delta_vs_allocated"] == -1
    assert result["building"]["sharing_ratio_pooled"] == 2.0  # 6 people / 3


def test_failure_rate_is_a_parameter():
    demands = np.array([0, 1, 1, 2, 2, 2, 3, 3, 4, 8])
    assert int(np.percentile(demands, 95, method="higher")) == 8
    assert int(np.percentile(demands, 80, method="higher")) == 4


def test_tipping_point_recovered_from_kinked_data():
    rng = np.random.default_rng(1)
    x = np.linspace(30, 95, 40)
    y = np.where(x <= 70, 4.2 - 0.002 * x, 4.2 - 0.002 * 70 - 0.08 * (x - 70))
    y = y + rng.normal(0, 0.02, len(x))
    result = find_tipping_point(x, y)
    assert result is not None and result["confident"]
    assert 60 <= result["tipping_point_x"] <= 80
    assert result["slope_after"] < result["slope_before"]


def test_tipping_point_needs_enough_points():
    assert find_tipping_point(np.array([1, 2, 3]), np.array([1, 2, 3])) is None
