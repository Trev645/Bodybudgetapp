"""Ingestion: normalise per-building uploads into the shared schema.

Clients upload one building at a time: building map (settings), teams,
headcount, space schedule, then per-study observation rounds and the
experience survey. Every loader is tenant-scoped — the authenticated
client_id is stamped on every row — and returns an IngestReport whose
issues are also persisted for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import Engine, text

from awa.ingestion import cleaning
from awa.ingestion.cleaning import Issue
from awa.security import new_id


@dataclass
class IngestReport:
    rows_ingested: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict:
        return {
            "rows_ingested": self.rows_ingested,
            "ok": self.ok,
            "issues": [vars(i) for i in self.issues],
        }


def _persist_issues(engine: Engine, client_id: str, study_id: str | None,
                    issues: list[Issue]) -> None:
    if not issues:
        return
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        for i in issues:
            conn.execute(
                text("INSERT INTO ingestion_issue (client_id, study_id, severity, "
                     "category, detail, created_at) VALUES (:c, :s, :v, :g, :d, :t)"),
                {"c": client_id, "s": study_id, "v": i.severity,
                 "g": i.category, "d": i.detail, "t": now},
            )


def _team_lookup(engine: Engine, building_id: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT team_id, name FROM team WHERE building_id = :b"),
            {"b": building_id}).fetchall()
    return {r.name.lower(): r.team_id for r in rows}


def _ensure_team(engine: Engine, client_id: str, building_id: str,
                 name: str, lookup: dict[str, str]) -> str:
    key = name.lower()
    if key not in lookup:
        team_id = new_id("team")
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO team (team_id, building_id, client_id, name) "
                     "VALUES (:i, :b, :c, :n)"),
                {"i": team_id, "b": building_id, "c": client_id, "n": name})
        lookup[key] = team_id
    return lookup[key]


def ingest_teams(engine: Engine, client_id: str, building_id: str,
                 df: pd.DataFrame) -> IngestReport:
    """Team list with desk allocations. Columns: team, allocated_desks."""
    report = IngestReport()
    df = cleaning.normalise_columns(df)
    if "name" in df.columns and "team" not in df.columns:
        df = df.rename(columns={"name": "team"})
    for err in cleaning.require_columns(df, ["team"], "teams"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, None, report.issues)
        return report
    df["team"] = df["team"].map(cleaning.normalise_label)
    df = df[df["team"].notna()]
    df = cleaning.drop_duplicates(df, ["team"], "team", report.issues)
    lookup = _team_lookup(engine, building_id)
    with engine.begin() as conn:
        for row in df.itertuples():
            desks = int(row.allocated_desks) if "allocated_desks" in df.columns \
                and pd.notna(row.allocated_desks) else 0
            key = row.team.lower()
            if key in lookup:
                conn.execute(
                    text("UPDATE team SET allocated_desks = :d WHERE team_id = :i"),
                    {"d": desks, "i": lookup[key]})
            else:
                team_id = new_id("team")
                conn.execute(
                    text("INSERT INTO team (team_id, building_id, client_id, name, "
                         "allocated_desks) VALUES (:i, :b, :c, :n, :d)"),
                    {"i": team_id, "b": building_id, "c": client_id,
                     "n": row.team, "d": desks})
                lookup[key] = team_id
            report.rows_ingested += 1
    _persist_issues(engine, client_id, None, report.issues)
    return report


def ingest_settings(engine: Engine, client_id: str, building_id: str,
                    df: pd.DataFrame) -> IngestReport:
    """Building map: every observable setting tagged to floor, zone and team.

    Columns: setting_code, setting_type, floor, zone, capacity, team, area_m2.
    """
    report = IngestReport()
    df = cleaning.normalise_columns(df)
    for err in cleaning.require_columns(df, ["setting_code", "setting_type"],
                                        "building map"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, None, report.issues)
        return report

    df = cleaning.fill_merged_cells(df, ["floor", "zone", "team", "setting_type"],
                                    report.issues)
    df["setting_code"] = df["setting_code"].map(cleaning.normalise_label)
    df = df[df["setting_code"].notna()]
    df = cleaning.drop_duplicates(df, ["setting_code"], "setting", report.issues)
    df["setting_type"] = (df["setting_type"].map(cleaning.normalise_label)
                          .str.lower().str.replace(" ", "_"))

    lookup = _team_lookup(engine, building_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM setting WHERE building_id = :b"),
                     {"b": building_id})
    for row in df.itertuples():
        team_name = cleaning.normalise_label(getattr(row, "team", None)) \
            if "team" in df.columns else None
        team_id = _ensure_team(engine, client_id, building_id, team_name, lookup) \
            if team_name else None
        capacity = int(row.capacity) if "capacity" in df.columns \
            and pd.notna(getattr(row, "capacity", None)) else 1
        area = float(row.area_m2) if "area_m2" in df.columns \
            and pd.notna(getattr(row, "area_m2", None)) else None
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO setting (setting_id, building_id, client_id, code, "
                     "type, floor, zone, capacity, allocated_team_id, area_m2) "
                     "VALUES (:i, :b, :c, :code, :ty, :f, :z, :cap, :team, :a)"),
                {"i": new_id("set"), "b": building_id, "c": client_id,
                 "code": row.setting_code, "ty": row.setting_type,
                 "f": cleaning.normalise_label(getattr(row, "floor", None)),
                 "z": cleaning.normalise_label(getattr(row, "zone", None)),
                 "cap": capacity, "team": team_id, "a": area})
        report.rows_ingested += 1

    # If desk allocations weren't supplied explicitly, derive them from the map.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE team SET allocated_desks = ("
            "  SELECT COUNT(*) FROM setting s "
            "  WHERE s.allocated_team_id = team.team_id AND s.type = 'desk') "
            "WHERE building_id = :b AND allocated_desks = 0"), {"b": building_id})

    _persist_issues(engine, client_id, None, report.issues)
    return report


def ingest_headcount(engine: Engine, client_id: str, building_id: str,
                     df: pd.DataFrame) -> IngestReport:
    """Who is nominally based in the building.

    Columns: person_ref, team, employment_type (employee/contractor).
    person_ref should be a pseudonymous reference, never a name or email.
    """
    report = IngestReport()
    df = cleaning.normalise_columns(df)
    for err in cleaning.require_columns(df, ["person_ref"], "headcount"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, None, report.issues)
        return report
    df = cleaning.fill_merged_cells(df, ["team", "employment_type"], report.issues)
    df["person_ref"] = df["person_ref"].map(cleaning.normalise_label)
    df = df[df["person_ref"].notna()]
    if df["person_ref"].str.contains("@").any():
        report.issues.append(Issue(
            "warning", "pii",
            "person_ref values look like email addresses; use pseudonymous "
            "references — the platform never needs identifiable people"))
    df = cleaning.drop_duplicates(df, ["person_ref"], "headcount", report.issues)
    if "employment_type" in df.columns:
        df["employment_type"] = cleaning.normalise_employment(
            df["employment_type"], report.issues)
    else:
        df["employment_type"] = "employee"
        report.issues.append(Issue(
            "info", "defaulted", "no employment_type column; all assumed employees"))

    lookup = _team_lookup(engine, building_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM person_assignment WHERE building_id = :b"),
                     {"b": building_id})
    for row in df.itertuples():
        team_name = cleaning.normalise_label(getattr(row, "team", None)) \
            if "team" in df.columns else None
        team_id = _ensure_team(engine, client_id, building_id, team_name, lookup) \
            if team_name else None
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO person_assignment (person_ref, building_id, "
                     "client_id, team_id, employment_type) "
                     "VALUES (:p, :b, :c, :t, :e)"),
                {"p": row.person_ref, "b": building_id, "c": client_id,
                 "t": team_id, "e": row.employment_type})
        report.rows_ingested += 1
    _persist_issues(engine, client_id, None, report.issues)
    return report


def ingest_space_schedule(engine: Engine, client_id: str, building_id: str,
                          df: pd.DataFrame) -> IngestReport:
    """Structured area breakdown by function (spec 3.3 — no PDF inference)."""
    report = IngestReport()
    df = cleaning.normalise_columns(df)
    for err in cleaning.require_columns(df, ["function", "area_m2"],
                                        "space schedule"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, None, report.issues)
        return report
    df["function"] = (df["function"].map(cleaning.normalise_label)
                      .str.lower().str.replace(" ", "_"))
    df = df[df["function"].notna() & pd.to_numeric(df["area_m2"], errors="coerce").notna()]
    df = cleaning.drop_duplicates(df, ["function"], "space schedule", report.issues)
    total = float(pd.to_numeric(df["area_m2"]).sum())
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM space_schedule WHERE building_id = :b"),
                     {"b": building_id})
        for row in df.itertuples():
            area = float(row.area_m2)
            pct = float(row.pct_of_nia) if "pct_of_nia" in df.columns \
                and pd.notna(getattr(row, "pct_of_nia", None)) \
                else (round(100 * area / total, 2) if total else None)
            conn.execute(
                text("INSERT INTO space_schedule (building_id, client_id, function, "
                     "area_m2, pct_of_nia) VALUES (:b, :c, :f, :a, :p)"),
                {"b": building_id, "c": client_id, "f": row.function,
                 "a": area, "p": pct})
            report.rows_ingested += 1
    _persist_issues(engine, client_id, None, report.issues)
    return report


def create_study(engine: Engine, client_id: str, building_id: str, *,
                 start_date: str, end_date: str, interval_mins: int) -> str:
    study_id = new_id("study")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO study (study_id, building_id, client_id, start_date, "
                 "end_date, interval_mins, created_at) "
                 "VALUES (:i, :b, :c, :s, :e, :m, :t)"),
            {"i": study_id, "b": building_id, "c": client_id, "s": start_date,
             "e": end_date, "m": interval_mins,
             "t": datetime.now(timezone.utc).isoformat()})
    return study_id


def ingest_observations(engine: Engine, client_id: str, study_id: str,
                        df: pd.DataFrame) -> IngestReport:
    """One or more observation rounds for a study.

    Columns: setting_code, ts, round_label, status, optionally team and
    occupant_count. Creates rounds, validates settings, flags gaps.
    """
    report = IngestReport()
    with engine.connect() as conn:
        study = conn.execute(
            text("SELECT building_id, start_date, end_date FROM study "
                 "WHERE study_id = :s"), {"s": study_id}).fetchone()
    building_id = study.building_id

    df = cleaning.normalise_columns(df)
    for err in cleaning.require_columns(
            df, ["setting_code", "ts", "round_label", "status"], "observations"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, study_id, report.issues)
        return report

    df = cleaning.fill_merged_cells(df, ["round_label", "ts"], report.issues)
    df["setting_code"] = df["setting_code"].map(cleaning.normalise_label)
    df["round_label"] = df["round_label"].map(cleaning.normalise_label)
    df = df[df["setting_code"].notna() & df["round_label"].notna()]
    df = cleaning.parse_timestamps(df, "ts", report.issues)
    df = cleaning.normalise_status(df, report.issues)
    df = cleaning.drop_duplicates(df, ["setting_code", "round_label"],
                                  "observation", report.issues)

    # Timestamps must sit inside the study window.
    start = pd.Timestamp(study.start_date)
    end = pd.Timestamp(study.end_date) + pd.Timedelta(days=1)
    outside = (df["ts"] < start) | (df["ts"] >= end)
    if outside.any():
        report.issues.append(Issue(
            "warning", "out_of_window",
            f"{int(outside.sum())} observation(s) timestamped outside the study "
            f"window ({study.start_date} to {study.end_date}) were dropped"))
        df = df[~outside]

    # Settings must exist in the building map.
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT setting_id, code FROM setting WHERE building_id = :b"),
            {"b": building_id}).fetchall()
    setting_ids = {r.code.lower(): r.setting_id for r in rows}
    unknown = ~df["setting_code"].str.lower().isin(setting_ids)
    if unknown.any():
        codes = sorted(df.loc[unknown, "setting_code"].unique())[:10]
        report.issues.append(Issue(
            "error", "unknown_setting",
            f"{int(unknown.sum())} observation(s) reference settings not in the "
            f"building map (e.g. {codes}); upload/fix the building map first"))
        df = df[~unknown]

    team_lookup = _team_lookup(engine, building_id)

    # Create rounds (one sweep of the building at a point in time).
    round_ids: dict[str, str] = {}
    with engine.connect() as conn:
        for r in conn.execute(text(
                "SELECT round_id, label FROM round WHERE study_id = :s"),
                {"s": study_id}).fetchall():
            round_ids[r.label] = r.round_id
    for label, group in df.groupby("round_label"):
        if label in round_ids:
            continue
        ts = group["ts"].min()
        rid = new_id("rnd")
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO round (round_id, study_id, client_id, label, ts, "
                     "day_of_week) VALUES (:i, :s, :c, :l, :t, :d)"),
                {"i": rid, "s": study_id, "c": client_id, "l": label,
                 "t": ts.isoformat(), "d": ts.day_name()})
        round_ids[label] = rid

    rows_out = []
    for row in df.itertuples():
        team_name = cleaning.normalise_label(getattr(row, "team", None)) \
            if "team" in df.columns else None
        team_id = _ensure_team(engine, client_id, building_id, team_name,
                               team_lookup) if team_name else None
        count = int(row.occupant_count) if "occupant_count" in df.columns \
            and pd.notna(getattr(row, "occupant_count", None)) else None
        rows_out.append({
            "set": setting_ids[row.setting_code.lower()],
            "rnd": round_ids[row.round_label],
            "st": study_id, "c": client_id, "t": row.ts.isoformat(),
            "status": row.status, "occ": 1 if row.status == "occupied" else 0,
            "n": count, "team": team_id})
    uploaded_rounds = {round_ids[l] for l in df["round_label"].unique()}
    with engine.begin() as conn:
        for rid in uploaded_rounds:  # re-uploading a round replaces it
            conn.execute(text("DELETE FROM observation WHERE round_id = :r"),
                         {"r": rid})
        for r in rows_out:
            conn.execute(
                text("INSERT INTO observation (setting_id, round_id, study_id, "
                     "client_id, ts, status, occupied, occupant_count, "
                     "occupant_team_id) "
                     "VALUES (:set, :rnd, :st, :c, :t, :status, :occ, :n, :team)"),
                r)
    report.rows_ingested = len(rows_out)

    # Completeness: every setting should be observed in every round.
    n_settings = len(setting_ids)
    with engine.connect() as conn:
        gaps = conn.execute(text(
            "SELECT r.label, COUNT(o.observation_id) AS seen FROM round r "
            "LEFT JOIN observation o ON o.round_id = r.round_id "
            "WHERE r.study_id = :s GROUP BY r.round_id, r.label"),
            {"s": study_id}).fetchall()
    for g in gaps:
        if g.seen < n_settings:
            report.issues.append(Issue(
                "warning", "missing_round_coverage",
                f"round '{g.label}': only {g.seen}/{n_settings} settings observed "
                f"— observer may have missed part of the sweep"))

    with engine.begin() as conn:
        conn.execute(text("UPDATE study SET status = :st WHERE study_id = :s"),
                     {"st": "validated" if report.ok else "ingested", "s": study_id})
    _persist_issues(engine, client_id, study_id, report.issues)
    return report


def ingest_survey(engine: Engine, client_id: str, study_id: str,
                  df: pd.DataFrame) -> IngestReport:
    """Experience perception survey joined to a study (spec 4.5).

    Columns: response_id, team, item, score (1-5).
    """
    report = IngestReport()
    with engine.connect() as conn:
        building_id = conn.execute(
            text("SELECT building_id FROM study WHERE study_id = :s"),
            {"s": study_id}).fetchone().building_id
    df = cleaning.normalise_columns(df)
    for err in cleaning.require_columns(df, ["response_id", "item", "score"],
                                        "experience survey"):
        report.issues.append(Issue("error", "missing_column", err))
        _persist_issues(engine, client_id, study_id, report.issues)
        return report
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    bad = df["score"].isna() | ~df["score"].between(1, 5)
    if bad.any():
        report.issues.append(Issue(
            "warning", "bad_score",
            f"{int(bad.sum())} response(s) with scores outside 1-5 dropped"))
        df = df[~bad]
    df["item"] = (df["item"].map(cleaning.normalise_label)
                  .str.lower().str.replace(" ", "_"))
    df["response_id"] = df["response_id"].map(cleaning.normalise_label)
    df = cleaning.drop_duplicates(df, ["response_id", "item"], "survey",
                                  report.issues)
    team_lookup = _team_lookup(engine, building_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM experience_survey WHERE study_id = :s"),
                     {"s": study_id})
        for row in df.itertuples():
            team_name = cleaning.normalise_label(getattr(row, "team", None)) \
                if "team" in df.columns else None
            team_id = _ensure_team(engine, client_id, building_id, team_name,
                                   team_lookup) if team_name else None
            conn.execute(
                text("INSERT INTO experience_survey (response_id, study_id, "
                     "client_id, team_id, item, score) "
                     "VALUES (:r, :s, :c, :t, :i, :sc)"),
                {"r": row.response_id, "s": study_id, "c": client_id,
                 "t": team_id, "i": row.item, "sc": float(row.score)})
            report.rows_ingested += 1
    _persist_issues(engine, client_id, study_id, report.issues)
    return report
