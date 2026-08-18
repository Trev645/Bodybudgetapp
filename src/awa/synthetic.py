"""Synthetic corpus generator.

Builds a realistic multi-client corpus through the real ingestion path
(so cleaning, validation and lineage are exercised end-to-end). Teams are
given different capacity pressure so the corpus contains a genuine
utilisation-experience relationship with a soft tipping point around
UTIL_TIPPING_PCT — letting the research engine be validated against a
known ground truth.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from awa import ingestion
from awa.security import create_client, new_id, size_band_for_area
from sqlalchemy import text
from datetime import datetime, timezone

UTIL_TIPPING_PCT = 70.0

SECTORS = ["finance", "technology", "professional services", "public sector"]
REGIONS = ["london", "manchester", "edinburgh"]
SURVEY_ITEMS = ["find_place_to_work", "sit_with_team", "focus_support"]


def _make_building(engine: Engine, client_id: str, name: str, region: str,
                   area: float) -> str:
    building_id = new_id("bld")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO building (building_id, client_id, name, gross_area_m2, "
            "region, size_band, created_at) VALUES (:i, :c, :n, :a, :r, :s, :t)"),
            {"i": building_id, "c": client_id, "n": name, "a": area, "r": region,
             "s": size_band_for_area(area),
             "t": datetime.now(timezone.utc).isoformat()})
    return building_id


def seed_demo(engine: Engine, n_clients: int = 6, seed: int = 42,
              days: int = 10, rounds_per_day: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    summary = {"clients": []}

    for ci in range(n_clients):
        sector = SECTORS[ci % len(SECTORS)]
        region = REGIONS[ci % len(REGIONS)]
        client_id, api_key = create_client(
            engine, name=f"Demo Client {ci + 1}", sector=sector,
            size_band="medium", region=region)
        area = float(rng.integers(6_000, 28_000))
        building_id = _make_building(engine, client_id,
                                     f"HQ Building {ci + 1}", region, area)

        n_teams = int(rng.integers(4, 8))
        teams, settings, headcount = [], [], []
        # Capacity pressure varies by team so the corpus spans the curve.
        pressures = rng.uniform(0.35, 0.95, n_teams)
        for ti in range(n_teams):
            team = f"Team {chr(65 + ti)}"
            people = int(rng.integers(15, 45))
            desks = max(4, int(round(people * pressures[ti])))
            teams.append({"team": team, "allocated_desks": desks})
            for d in range(desks):
                settings.append({
                    "setting_code": f"D-{ti + 1}-{d + 1:03d}", "type": "desk",
                    "floor": str(1 + ti // 3), "zone": f"Zone {ti + 1}",
                    "capacity": 1, "team": team})
            for p in range(people):
                headcount.append({
                    "person_ref": f"P{ti:02d}{p:03d}", "team": team,
                    "employment_type": "employee" if rng.random() < 0.85
                    else "contractor"})
        for m in range(int(rng.integers(4, 9))):
            settings.append({
                "setting_code": f"MR-{m + 1:02d}", "type": "meeting room",
                "floor": "1", "zone": "Core",
                "capacity": int(rng.choice([4, 6, 8, 12])), "team": None})

        ingestion.ingest_teams(engine, client_id, building_id,
                               pd.DataFrame(teams))
        ingestion.ingest_settings(engine, client_id, building_id,
                                  pd.DataFrame(settings))
        ingestion.ingest_headcount(engine, client_id, building_id,
                                   pd.DataFrame(headcount))
        ingestion.ingest_space_schedule(engine, client_id, building_id,
                                        pd.DataFrame([
            {"function": "desks", "area_m2": area * 0.45},
            {"function": "meeting", "area_m2": area * 0.15},
            {"function": "breakout", "area_m2": area * 0.12},
            {"function": "circulation", "area_m2": area * 0.28}]))

        start = pd.Timestamp("2026-03-02")
        study_id = ingestion.create_study(
            engine, client_id, building_id,
            start_date=str(start.date()),
            end_date=str((start + pd.Timedelta(days=days + 3)).date()),
            interval_mins=120)

        obs_rows = []
        team_util = {}
        business_days = pd.bdate_range(start, periods=days)
        hours = [10, 12, 15][:rounds_per_day]
        for day in business_days:
            attendance = rng.uniform(0.45, 0.75)
            for hour in hours:
                ts = day + pd.Timedelta(hours=hour)
                label = f"{day.strftime('%a %d %b')} {hour:02d}:00"
                for ti, t in enumerate(teams):
                    people = sum(1 for h in headcount if h["team"] == t["team"])
                    present = rng.binomial(people, attendance)
                    at_desk = rng.binomial(present, 0.65 if hour != 12 else 0.45)
                    demand = min(at_desk, t["allocated_desks"])
                    claimed = min(rng.binomial(t["allocated_desks"] - demand, 0.15)
                                  if t["allocated_desks"] > demand else 0,
                                  t["allocated_desks"] - demand)
                    team_util.setdefault(t["team"], []).append(
                        100 * demand / t["allocated_desks"])
                    for d in range(t["allocated_desks"]):
                        status = ("occupied" if d < demand else
                                  "claimed" if d < demand + claimed else "empty")
                        obs_rows.append({
                            "setting_code": f"D-{ti + 1}-{d + 1:03d}",
                            "ts": ts.isoformat(), "round": label,
                            "status": status,
                            "team": t["team"] if status == "occupied" else None})
                for s in settings:
                    if s["type"] != "meeting room":
                        continue
                    used = rng.random() < 0.5
                    claimed_room = (not used) and rng.random() < 0.2
                    obs_rows.append({
                        "setting_code": s["setting_code"], "ts": ts.isoformat(),
                        "round": label,
                        "status": ("occupied" if used else
                                   "claimed" if claimed_room else "empty"),
                        "occupant_count": int(rng.integers(
                            2, max(3, s["capacity"]))) if used else None,
                        "team": None})
        ingestion.ingest_observations(engine, client_id, study_id,
                                      pd.DataFrame(obs_rows))

        # Experience responds to utilisation pressure with a kink at the
        # tipping point: gentle drift below it, steep decline above it.
        survey_rows = []
        for t in teams:
            util = float(np.mean(team_util[t["team"]]))
            base = 4.3 - 0.004 * util
            if util > UTIL_TIPPING_PCT:
                base -= 0.05 * (util - UTIL_TIPPING_PCT)
            for r in range(12):
                for item in SURVEY_ITEMS:
                    score = np.clip(rng.normal(base, 0.35), 1, 5)
                    survey_rows.append({
                        "response_id": f"{t['team']}-{r:02d}",
                        "team": t["team"], "item": item,
                        "score": round(float(score), 1)})
        ingestion.ingest_survey(engine, client_id, study_id,
                                pd.DataFrame(survey_rows))

        summary["clients"].append({
            "client_id": client_id, "api_key": api_key, "sector": sector,
            "region": region, "building_id": building_id, "study_id": study_id})
    return summary
