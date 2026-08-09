import os

import pytest

import awa.db as db


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AWA_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    db.reset_engine()
    eng = db.get_engine()
    db.run_migrations(eng)
    yield eng
    db.reset_engine()
