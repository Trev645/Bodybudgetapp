import pandas as pd

from awa.ingestion import cleaning


def test_column_synonyms_normalised():
    df = pd.DataFrame(columns=["Desk ID", "Time", "Round", "Occupancy Status",
                               "Occupant Team"])
    out = cleaning.normalise_columns(df)
    assert list(out.columns) == ["setting_code", "ts", "round_label",
                                 "status", "team"]


def test_merged_cells_forward_filled_and_flagged():
    issues = []
    df = pd.DataFrame({"floor": ["1", None, None, "2", None]})
    out = cleaning.fill_merged_cells(df, ["floor"], issues)
    assert list(out["floor"]) == ["1", "1", "1", "2", "2"]
    assert issues and issues[0].category == "merged_cells"


def test_status_labels_mapped_and_unknowns_dropped():
    issues = []
    df = pd.DataFrame({"status": ["Occupied", "SIGNS OF LIFE", "vacant",
                                  "banana", "Y"]})
    out = cleaning.normalise_status(df, issues)
    assert list(out["status"]) == ["occupied", "claimed", "empty", "occupied"]
    assert any(i.category == "unknown_status" and i.severity == "error"
               for i in issues)


def test_inconsistent_labels_collapse():
    assert cleaning.normalise_label("  Desk   A-01 ") == "Desk A-01"
    assert cleaning.normalise_label(float("nan")) is None
