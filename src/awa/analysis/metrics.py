"""Core utilisation metrics (spec 4.1).

Definitions:
- Peak occupancy: highest % of settings in use in any single round.
- Average utilisation: mean % occupied across all rounds.
- Frequency: how often a setting shows any use (occupied OR claimed).
- Occupancy: someone actually present. The frequency-occupancy gap is
  where the insight lives — desks claimed but empty.

Meeting rooms behave differently from desks and are reported by the
separate meeting_room_module().
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine


def load_observations(engine: Engine, study_id: str) -> pd.DataFrame:
    """Observations joined to setting type and round, for one study."""
    from sqlalchemy import text
    return pd.read_sql(
        text("""
        SELECT o.observation_id, o.setting_id, s.code AS setting_code,
               s.type AS setting_type, s.capacity, s.allocated_team_id,
               o.round_id, r.label AS round_label, r.ts AS round_ts,
               r.day_of_week, o.status, o.occupied, o.occupant_count,
               o.occupant_team_id
        FROM observation o
        JOIN setting s ON s.setting_id = o.setting_id
        JOIN round r ON r.round_id = o.round_id
        WHERE o.study_id = :study_id
        """),
        engine, params={"study_id": study_id})


def _round_rates(df: pd.DataFrame) -> pd.DataFrame:
    """% of settings occupied / in use, per round."""
    grouped = df.groupby(["round_id", "round_label", "day_of_week"]).agg(
        settings=("observation_id", "count"),
        occupied=("occupied", "sum"),
        in_use=("status", lambda s: int((s != "empty").sum())),
    ).reset_index()
    grouped["occupancy_pct"] = 100 * grouped["occupied"] / grouped["settings"]
    grouped["in_use_pct"] = 100 * grouped["in_use"] / grouped["settings"]
    return grouped


def utilisation_summary(df: pd.DataFrame, setting_type: str = "desk") -> dict:
    """Headline metrics for one setting type across a study."""
    sub = df[df["setting_type"] == setting_type]
    if sub.empty:
        return {"setting_type": setting_type, "settings": 0, "rounds": 0,
                "note": f"no observations for setting type '{setting_type}'"}
    rounds = _round_rates(sub)
    per_setting = per_setting_rates(sub)
    peak_row = rounds.loc[rounds["occupancy_pct"].idxmax()]
    return {
        "setting_type": setting_type,
        "settings": int(sub["setting_id"].nunique()),
        "rounds": int(rounds.shape[0]),
        "peak_occupancy_pct": round(float(rounds["occupancy_pct"].max()), 1),
        "peak_round": str(peak_row["round_label"]),
        "average_utilisation_pct": round(float(rounds["occupancy_pct"].mean()), 1),
        "average_in_use_pct": round(float(rounds["in_use_pct"].mean()), 1),
        "frequency_pct": round(float(per_setting["frequency_pct"].mean()), 1),
        "occupancy_pct": round(float(per_setting["occupancy_pct"].mean()), 1),
        "claimed_but_empty_pct": round(
            float((per_setting["frequency_pct"]
                   - per_setting["occupancy_pct"]).mean()), 1),
        "by_day": {
            day: round(float(g["occupancy_pct"].mean()), 1)
            for day, g in rounds.groupby("day_of_week")
        },
    }


def round_series(df: pd.DataFrame, setting_type: str = "desk") -> list[dict]:
    """Chronological per-round occupancy series (for the sweep strip)."""
    sub = df[df["setting_type"] == setting_type]
    if sub.empty:
        return []
    rounds = _round_rates(sub)
    ts = sub.groupby("round_id")["round_ts"].first()
    rounds = rounds.assign(ts=rounds["round_id"].map(ts)).sort_values("ts")
    return [
        {"label": r.round_label, "ts": str(r.ts), "day": r.day_of_week,
         "occupancy_pct": round(float(r.occupancy_pct), 1),
         "in_use_pct": round(float(r.in_use_pct), 1)}
        for r in rounds.itertuples()
    ]


def per_setting_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Frequency vs occupancy per individual setting."""
    out = df.groupby(["setting_id", "setting_code"]).agg(
        rounds=("observation_id", "count"),
        occupied_rounds=("occupied", "sum"),
        used_rounds=("status", lambda s: int((s != "empty").sum())),
    ).reset_index()
    out["occupancy_pct"] = 100 * out["occupied_rounds"] / out["rounds"]
    out["frequency_pct"] = 100 * out["used_rounds"] / out["rounds"]
    out["claimed_gap_pct"] = out["frequency_pct"] - out["occupancy_pct"]
    return out


def meeting_room_module(df: pd.DataFrame) -> dict:
    """Distinct analysis module for meeting rooms (spec 4.1).

    Booked-vs-used needs a booking-system feed (future enhancement); the
    observable proxies here are the claimed-but-empty rate (signs of a
    booking, nobody present) and size-vs-need mismatch where occupant
    counts were recorded.
    """
    rooms = df[df["setting_type"] == "meeting_room"]
    if rooms.empty:
        return {"rooms": 0, "note": "no meeting rooms observed"}
    summary = utilisation_summary(df, "meeting_room")
    result = {
        "rooms": summary["settings"],
        "peak_occupancy_pct": summary["peak_occupancy_pct"],
        "average_utilisation_pct": summary["average_utilisation_pct"],
        "no_show_proxy_pct": summary["claimed_but_empty_pct"],
    }
    counted = rooms[rooms["occupied"] == 1].dropna(subset=["occupant_count"])
    if not counted.empty:
        mismatch = counted.groupby(["setting_id", "setting_code", "capacity"]).agg(
            avg_occupants=("occupant_count", "mean")).reset_index()
        mismatch["fill_pct"] = 100 * mismatch["avg_occupants"] / mismatch["capacity"]
        result["size_vs_need"] = {
            "avg_fill_pct_when_used": round(float(mismatch["fill_pct"].mean()), 1),
            "rooms_under_half_full": int((mismatch["fill_pct"] < 50).sum()),
            "worst_fit_rooms": mismatch.nsmallest(5, "fill_pct")[
                ["setting_code", "capacity", "avg_occupants", "fill_pct"]
            ].round(1).to_dict("records"),
        }
    else:
        result["size_vs_need"] = "no occupant counts recorded for rooms"
    result["booked_vs_used"] = ("requires booking-system feed; "
                                "no_show_proxy_pct uses claimed-but-empty rate")
    return result
