"""Cleaning and normalisation for messy client spreadsheets (spec 2.1, 3.2).

Handles the three classic failure modes before any data is trusted:
merged cells (blank runs under a value), inconsistent labels, and gaps
where an observer missed a round. Problems become Issue records, not
silent fixes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd


@dataclass
class Issue:
    severity: str   # info / warning / error
    category: str
    detail: str


# Column-name synonyms seen across client formats → canonical names.
COLUMN_SYNONYMS = {
    "desk": "setting_code", "desk_id": "setting_code", "desk_ref": "setting_code",
    "setting": "setting_code", "setting_id": "setting_code", "space": "setting_code",
    "space_id": "setting_code", "room": "setting_code", "code": "setting_code",
    "time": "ts", "timestamp": "ts", "datetime": "ts", "date_time": "ts",
    "observation_time": "ts",
    "round": "round_label", "round_id": "round_label", "sweep": "round_label",
    "occupancy": "status", "state": "status", "occupied": "status",
    "occupancy_status": "status",
    "team_name": "team", "occupant_team": "team", "department": "team",
    "dept": "team", "org_unit": "team",
    "people": "occupant_count", "headcount": "occupant_count",
    "occupants": "occupant_count", "persons": "occupant_count",
    "type": "setting_type", "space_type": "setting_type",
    "employee_id": "person_ref", "staff_id": "person_ref", "person": "person_ref",
    "person_id": "person_ref",
    "employment": "employment_type", "worker_type": "employment_type",
    "contract_type": "employment_type",
    "level": "floor", "storey": "floor",
    "area": "area_m2", "sqm": "area_m2", "area_sqm": "area_m2", "nia_m2": "area_m2",
    "seats": "capacity",
    "desks": "allocated_desks", "desk_allocation": "allocated_desks",
    "score_value": "score", "rating": "score",
    "question": "item", "survey_item": "item",
    "respondent": "response_id", "respondent_id": "response_id",
    "function_name": "function", "use": "function",
}

STATUS_MAP = {
    "occupied": "occupied", "present": "occupied", "in use": "occupied",
    "person present": "occupied", "y": "occupied", "yes": "occupied",
    "1": "occupied", "true": "occupied", "occ": "occupied",
    "claimed": "claimed", "signs of life": "claimed", "signs of use": "claimed",
    "belongings": "claimed", "temporarily unoccupied": "claimed",
    "temp unoccupied": "claimed", "sol": "claimed",
    "empty": "empty", "vacant": "empty", "unoccupied": "empty", "free": "empty",
    "n": "empty", "no": "empty", "0": "empty", "false": "empty",
}

EMPLOYMENT_MAP = {
    "employee": "employee", "emp": "employee", "staff": "employee",
    "perm": "employee", "permanent": "employee", "fte": "employee",
    "contractor": "contractor", "contract": "contractor",
    "temp": "contractor", "consultant": "contractor", "agency": "contractor",
}


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lower/snake-case headers and map known synonyms to canonical names."""
    df = df.copy()
    cols = {}
    for col in df.columns:
        c = re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")
        cols[col] = COLUMN_SYNONYMS.get(c, c)
    return df.rename(columns=cols)


def normalise_label(value) -> str | None:
    """Trim and collapse whitespace; treat blanks/NaN as missing."""
    if pd.isna(value):
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def fill_merged_cells(df: pd.DataFrame, columns: list[str],
                      issues: list[Issue]) -> pd.DataFrame:
    """Forward-fill blank runs left behind by merged spreadsheet cells."""
    df = df.copy()
    for col in columns:
        if col not in df.columns:
            continue
        blanks = int(df[col].isna().sum())
        if blanks:
            df[col] = df[col].ffill()
            issues.append(Issue(
                "warning", "merged_cells",
                f"column '{col}': forward-filled {blanks} blank cell(s), "
                f"likely merged cells in the source spreadsheet"))
    return df


def normalise_status(df: pd.DataFrame, issues: list[Issue]) -> pd.DataFrame:
    """Map free-text occupancy labels onto occupied / claimed / empty."""
    df = df.copy()
    raw = df["status"].map(normalise_label).str.lower()
    df["status"] = raw.map(STATUS_MAP)
    bad = df["status"].isna() & raw.notna()
    if bad.any():
        labels = sorted(raw[bad].unique())[:10]
        issues.append(Issue(
            "error", "unknown_status",
            f"{int(bad.sum())} observation(s) with unrecognised status "
            f"label(s) {labels} were dropped"))
        df = df[~bad]
    missing = df["status"].isna()
    if missing.any():
        issues.append(Issue(
            "warning", "missing_status",
            f"{int(missing.sum())} observation(s) with no status recorded "
            f"were dropped"))
        df = df[~missing]
    return df


def normalise_employment(series: pd.Series, issues: list[Issue]) -> pd.Series:
    raw = series.map(normalise_label).str.lower()
    mapped = raw.map(EMPLOYMENT_MAP)
    bad = mapped.isna()
    if bad.any():
        issues.append(Issue(
            "warning", "unknown_employment_type",
            f"{int(bad.sum())} person(s) with unrecognised employment type "
            f"defaulted to 'employee'"))
        mapped = mapped.fillna("employee")
    return mapped


def parse_timestamps(df: pd.DataFrame, col: str, issues: list[Issue]) -> pd.DataFrame:
    df = df.copy()
    # ISO / unambiguous formats first; retry failures as UK-style day-first.
    parsed = pd.to_datetime(df[col], errors="coerce")
    retry = parsed.isna() & df[col].notna()
    if retry.any():
        parsed.loc[retry] = pd.to_datetime(df.loc[retry, col], errors="coerce",
                                           format="mixed", dayfirst=True)
    bad = parsed.isna() & df[col].notna()
    if bad.any():
        issues.append(Issue(
            "error", "bad_timestamp",
            f"{int(bad.sum())} row(s) with unparseable timestamps were dropped"))
    df[col] = parsed
    return df[parsed.notna()]


def drop_duplicates(df: pd.DataFrame, keys: list[str], what: str,
                    issues: list[Issue]) -> pd.DataFrame:
    dupes = df.duplicated(subset=keys, keep="first")
    if dupes.any():
        issues.append(Issue(
            "warning", "duplicates",
            f"{int(dupes.sum())} duplicate {what} row(s) dropped "
            f"(keyed on {', '.join(keys)})"))
    return df[~dupes]


def require_columns(df: pd.DataFrame, required: list[str],
                    what: str) -> list[str]:
    """Return human-readable errors for any required column that is absent."""
    missing = [c for c in required if c not in df.columns]
    if missing:
        return [f"{what} upload is missing required column(s): "
                f"{', '.join(missing)}. Found: {', '.join(map(str, df.columns))}"]
    return []
