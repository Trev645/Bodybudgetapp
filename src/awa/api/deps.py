"""Request authentication and tenant scoping.

Every route resolves the API key to a Principal; every data access is
filtered by the principal's client_id. Cross-tenant probes return 404
(not 403) so resource existence is never leaked across tenancies.
"""

from __future__ import annotations

import io

import pandas as pd
from fastapi import Depends, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
from sqlalchemy import Engine, text

from awa.db import get_engine
from awa.security import Principal, authenticate

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def engine() -> Engine:
    return get_engine()


def get_principal(raw_key: str | None = Security(api_key_header),
                  eng: Engine = Depends(engine)) -> Principal:
    principal = authenticate(eng, raw_key)
    if principal is None:
        raise HTTPException(401, "missing or invalid API key")
    return principal


def require_client(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.client_id is None:
        raise HTTPException(403, "this endpoint requires a client-scoped key")
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "admin key required")
    return principal


def require_uploads_enabled(eng: Engine, principal: Principal) -> None:
    """Uploads can be switched off per organisation from the admin console."""
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT uploads_enabled FROM client WHERE client_id = :c"),
            {"c": principal.client_id}).fetchone()
    if row is None or not row.uploads_enabled:
        raise HTTPException(
            403, "uploads are currently disabled for your organisation — "
                 "contact AWA to re-enable them")


def owned_building(eng: Engine, principal: Principal, building_id: str) -> None:
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT 1 FROM building WHERE building_id = :b AND client_id = :c"),
            {"b": building_id, "c": principal.client_id}).fetchone()
    if row is None:
        raise HTTPException(404, "building not found")


def owned_study(eng: Engine, principal: Principal, study_id: str) -> None:
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT 1 FROM study WHERE study_id = :s AND client_id = :c"),
            {"s": study_id, "c": principal.client_id}).fetchone()
    if row is None:
        raise HTTPException(404, "study not found")


async def read_tabular_upload(file: UploadFile) -> pd.DataFrame:
    """Parse an uploaded CSV/Excel file with a hard size limit."""
    blob = await file.read()
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload exceeds 25 MB limit")
    name = (file.filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            return pd.read_excel(io.BytesIO(blob))
        return pd.read_csv(io.BytesIO(blob))
    except Exception as exc:  # noqa: BLE001 - surface parse failures to client
        raise HTTPException(422, f"could not parse upload: {exc}") from exc
