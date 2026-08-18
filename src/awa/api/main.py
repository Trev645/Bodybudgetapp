"""AWA Utilisation Analytics & Benchmarking API.

Clients upload their own data one building at a time, run the full
analysis suite over their portfolio, and compare against anonymised
peers by sector, size band and/or region. Admin keys manage clients and
run corpus research.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from awa import __version__, ingestion
from awa.analysis import cascade, experience, metrics, sizing
from awa.api import deps
from awa.benchmarking.kpis import latest_study_id, study_kpis
from awa.benchmarking.peers import benchmark_building
from awa.benchmarking.research import capacity_experience_research
from awa.db import run_migrations
from awa.security import (Principal, create_client, issue_key, new_id,
                          size_band_for_area)

@asynccontextmanager
async def _lifespan(_: FastAPI):
    run_migrations()
    yield


app = FastAPI(
    title="AWA Utilisation & Benchmarking Platform",
    version=__version__,
    description="Workplace utilisation analytics: ingest, normalise, "
                "analyse and benchmark — from one building to a "
                "cross-client research corpus.",
    lifespan=_lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


WEB_INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"


@app.get("/", include_in_schema=False)
def console() -> HTMLResponse:
    """The client console: uploads, reports and peer comparison in one page."""
    return HTMLResponse(WEB_INDEX.read_text())


@app.get("/me")
def whoami(principal: Principal = Depends(deps.get_principal),
           eng: Engine = Depends(deps.engine)) -> dict:
    out: dict = {"role": principal.role}
    if principal.client_id:
        with eng.connect() as conn:
            row = conn.execute(text(
                "SELECT name, uploads_enabled FROM client WHERE client_id = :c"),
                {"c": principal.client_id}).fetchone()
        if row:
            out["organisation"] = row.name
            out["uploads_enabled"] = bool(row.uploads_enabled)
    return out


# --------------------------------------------------------------- admin

class NewClient(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sector: str = Field(min_length=1, max_length=100)
    size_band: str = Field(default="unknown", max_length=50)
    region: str = Field(min_length=1, max_length=100)


@app.post("/admin/clients", status_code=201)
def register_client(body: NewClient,
                    _: Principal = Depends(deps.require_admin),
                    eng: Engine = Depends(deps.engine)) -> dict:
    client_id, raw_key = create_client(
        eng, name=body.name, sector=body.sector,
        size_band=body.size_band, region=body.region)
    return {"client_id": client_id, "api_key": raw_key,
            "note": "store this key now; only its hash is retained"}


@app.get("/admin/clients")
def list_clients(_: Principal = Depends(deps.require_admin),
                 eng: Engine = Depends(deps.engine)) -> list[dict]:
    with eng.connect() as conn:
        rows = conn.execute(text("""
            SELECT c.client_id, c.name, c.sector, c.region, c.uploads_enabled,
                   (SELECT COUNT(*) FROM building b
                    WHERE b.client_id = c.client_id) AS buildings,
                   (SELECT COUNT(*) FROM study s
                    WHERE s.client_id = c.client_id
                      AND s.status != 'open') AS studies,
                   (SELECT COUNT(*) FROM api_key k
                    WHERE k.client_id = c.client_id AND k.revoked = 0)
                   AS active_keys
            FROM client c ORDER BY c.created_at
            """)).mappings().all()
    return [dict(r) | {"uploads_enabled": bool(r["uploads_enabled"])}
            for r in rows]


class ClientUpdate(BaseModel):
    uploads_enabled: bool


@app.patch("/admin/clients/{client_id}")
def update_client(client_id: str, body: ClientUpdate,
                  _: Principal = Depends(deps.require_admin),
                  eng: Engine = Depends(deps.engine)) -> dict:
    with eng.begin() as conn:
        result = conn.execute(text(
            "UPDATE client SET uploads_enabled = :u WHERE client_id = :c"),
            {"u": 1 if body.uploads_enabled else 0, "c": client_id})
    if result.rowcount == 0:
        raise HTTPException(404, "organisation not found")
    return {"client_id": client_id, "uploads_enabled": body.uploads_enabled}


@app.post("/admin/clients/{client_id}/reissue-key")
def reissue_key(client_id: str,
                _: Principal = Depends(deps.require_admin),
                eng: Engine = Depends(deps.engine)) -> dict:
    """Revoke every existing key for the organisation and issue a fresh one."""
    with eng.connect() as conn:
        client = conn.execute(text(
            "SELECT name FROM client WHERE client_id = :c"),
            {"c": client_id}).fetchone()
    if client is None:
        raise HTTPException(404, "organisation not found")
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE api_key SET revoked = 1 WHERE client_id = :c"),
            {"c": client_id})
    raw_key = issue_key(eng, client_id=client_id, role="client",
                        label=f"reissued key for {client.name}")
    return {"client_id": client_id, "api_key": raw_key,
            "note": "previous keys are revoked; share this one securely"}


@app.get("/admin/research/capacity-experience")
def research(_: Principal = Depends(deps.require_admin),
             eng: Engine = Depends(deps.engine)) -> dict:
    return capacity_experience_research(eng)


# ------------------------------------------------------------ buildings

class NewBuilding(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gross_area_m2: float | None = Field(default=None, gt=0)
    region: str = Field(min_length=1, max_length=100)


@app.post("/buildings", status_code=201)
def create_building(body: NewBuilding,
                    principal: Principal = Depends(deps.require_client),
                    eng: Engine = Depends(deps.engine)) -> dict:
    deps.require_uploads_enabled(eng, principal)
    building_id = new_id("bld")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO building (building_id, client_id, name, gross_area_m2, "
            "region, size_band, created_at) VALUES (:i, :c, :n, :a, :r, :s, :t)"),
            {"i": building_id, "c": principal.client_id, "n": body.name,
             "a": body.gross_area_m2, "r": body.region.strip().lower(),
             "s": size_band_for_area(body.gross_area_m2),
             "t": datetime.now(timezone.utc).isoformat()})
    return {"building_id": building_id,
            "size_band": size_band_for_area(body.gross_area_m2)}


@app.get("/buildings")
def list_buildings(principal: Principal = Depends(deps.require_client),
                   eng: Engine = Depends(deps.engine)) -> list[dict]:
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT building_id, name, gross_area_m2, region, size_band "
            "FROM building WHERE client_id = :c ORDER BY created_at"),
            {"c": principal.client_id}).mappings().all()
    return [dict(r) for r in rows]


def _upload_route(loader):
    async def route(building_id: str, file: UploadFile,
                    principal: Principal = Depends(deps.require_client),
                    eng: Engine = Depends(deps.engine)) -> dict:
        deps.owned_building(eng, principal, building_id)
        deps.require_uploads_enabled(eng, principal)
        df = await deps.read_tabular_upload(file)
        report = loader(eng, principal.client_id, building_id, df)
        return report.to_dict()
    return route


app.post("/buildings/{building_id}/settings")(
    _upload_route(ingestion.ingest_settings))
app.post("/buildings/{building_id}/teams")(
    _upload_route(ingestion.ingest_teams))
app.post("/buildings/{building_id}/headcount")(
    _upload_route(ingestion.ingest_headcount))
app.post("/buildings/{building_id}/space-schedule")(
    _upload_route(ingestion.ingest_space_schedule))


@app.get("/buildings/{building_id}/studies")
def list_studies(building_id: str,
                 principal: Principal = Depends(deps.require_client),
                 eng: Engine = Depends(deps.engine)) -> list[dict]:
    deps.owned_building(eng, principal, building_id)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT study_id, start_date, end_date, interval_mins, status "
            "FROM study WHERE building_id = :b ORDER BY start_date DESC"),
            {"b": building_id}).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- studies

class NewStudy(BaseModel):
    start_date: date
    end_date: date
    interval_mins: int = Field(default=60, ge=5, le=480)


@app.post("/buildings/{building_id}/studies", status_code=201)
def create_study(building_id: str, body: NewStudy,
                 principal: Principal = Depends(deps.require_client),
                 eng: Engine = Depends(deps.engine)) -> dict:
    deps.owned_building(eng, principal, building_id)
    deps.require_uploads_enabled(eng, principal)
    if body.end_date < body.start_date:
        raise HTTPException(422, "end_date must not precede start_date")
    study_id = ingestion.create_study(
        eng, principal.client_id, building_id,
        start_date=body.start_date.isoformat(),
        end_date=body.end_date.isoformat(),
        interval_mins=body.interval_mins)
    return {"study_id": study_id}


@app.post("/studies/{study_id}/observations")
async def upload_observations(study_id: str, file: UploadFile,
                              principal: Principal = Depends(deps.require_client),
                              eng: Engine = Depends(deps.engine)) -> dict:
    deps.owned_study(eng, principal, study_id)
    deps.require_uploads_enabled(eng, principal)
    df = await deps.read_tabular_upload(file)
    return ingestion.ingest_observations(
        eng, principal.client_id, study_id, df).to_dict()


@app.post("/studies/{study_id}/survey")
async def upload_survey(study_id: str, file: UploadFile,
                        principal: Principal = Depends(deps.require_client),
                        eng: Engine = Depends(deps.engine)) -> dict:
    deps.owned_study(eng, principal, study_id)
    deps.require_uploads_enabled(eng, principal)
    df = await deps.read_tabular_upload(file)
    return ingestion.ingest_survey(
        eng, principal.client_id, study_id, df).to_dict()


# -------------------------------------------------------------- analysis

@app.get("/studies/{study_id}/report")
def full_report(study_id: str,
                failure_rate: float = Query(default=5.0, gt=0, lt=100),
                principal: Principal = Depends(deps.require_client),
                eng: Engine = Depends(deps.engine)) -> dict:
    """The full analysis suite (spec Section 4) for one study."""
    deps.owned_study(eng, principal, study_id)
    obs = metrics.load_observations(eng, study_id)
    if obs.empty:
        raise HTTPException(409, "no observations ingested for this study yet")
    return {
        "desks": metrics.utilisation_summary(obs, "desk"),
        "desk_rounds": metrics.round_series(obs, "desk"),
        "meeting_rooms": metrics.meeting_room_module(obs),
        "desk_not_found_risk": sizing.desk_not_found_risk(eng, study_id),
        "desk_sizing": sizing.desks_required(eng, study_id, failure_rate),
        "headcount_cascade": cascade.headcount_cascade(eng, study_id),
        "experience": experience.experience_summary(eng, study_id),
        "experience_overlay": experience.experience_overlay(eng, study_id),
    }


@app.get("/studies/{study_id}/sizing")
def desk_sizing(study_id: str,
                failure_rate: float = Query(default=5.0, gt=0, lt=100),
                principal: Principal = Depends(deps.require_client),
                eng: Engine = Depends(deps.engine)) -> dict:
    deps.owned_study(eng, principal, study_id)
    return sizing.desks_required(eng, study_id, failure_rate)


@app.get("/studies/{study_id}/issues")
def study_issues(study_id: str,
                 principal: Principal = Depends(deps.require_client),
                 eng: Engine = Depends(deps.engine)) -> list[dict]:
    deps.owned_study(eng, principal, study_id)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT severity, category, detail, created_at FROM ingestion_issue "
            "WHERE study_id = :s AND client_id = :c ORDER BY issue_id"),
            {"s": study_id, "c": principal.client_id}).mappings().all()
    return [dict(r) for r in rows]


@app.get("/portfolio")
def portfolio(principal: Principal = Depends(deps.require_client),
              eng: Engine = Depends(deps.engine)) -> list[dict]:
    """Latest-study KPIs for every building the client has studied."""
    out = []
    with eng.connect() as conn:
        buildings = conn.execute(text(
            "SELECT building_id, name, region, size_band FROM building "
            "WHERE client_id = :c ORDER BY created_at"),
            {"c": principal.client_id}).fetchall()
    for b in buildings:
        sid = latest_study_id(eng, b.building_id)
        out.append({
            "building_id": b.building_id, "name": b.name,
            "region": b.region, "size_band": b.size_band,
            "latest_study_id": sid,
            "kpis": study_kpis(eng, sid) if sid else None,
        })
    return out


# ------------------------------------------------------------ benchmarking

@app.get("/buildings/{building_id}/benchmark")
def benchmark(building_id: str,
              dimensions: str = Query(
                  default="sector,size_band",
                  description="comma-separated subset of sector,size_band,region"),
              principal: Principal = Depends(deps.require_client),
              eng: Engine = Depends(deps.engine)) -> dict:
    deps.owned_building(eng, principal, building_id)
    try:
        return benchmark_building(eng, principal.client_id, building_id,
                                  dimensions.split(","))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
