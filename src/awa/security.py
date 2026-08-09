"""Tenancy and credential primitives.

Every request is authenticated by an API key (only its SHA-256 hash is
stored) and resolved to a Principal. Client principals are scoped to their
own client_id; admin principals may create clients and run corpus research.
Anonymisation of the pooled corpus is enforced at query time (see
benchmarking.MIN_PEER_CLIENTS) — client identities never leave their tenancy.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine, text

KEY_PREFIX = "awa_"

# Building size bands derived from gross area (m2). Used for peer matching.
SIZE_BANDS = [
    (5_000, "small"),
    (15_000, "medium"),
    (30_000, "large"),
    (float("inf"), "enterprise"),
]


def size_band_for_area(gross_area_m2: float | None) -> str:
    if not gross_area_m2 or gross_area_m2 <= 0:
        return "unknown"
    for upper, band in SIZE_BANDS:
        if gross_area_m2 < upper:
            return band
    return "enterprise"


@dataclass(frozen=True)
class Principal:
    role: str                 # 'client' | 'admin'
    client_id: str | None     # None for admin keys

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def issue_key(engine: Engine, *, client_id: str | None, role: str, label: str) -> str:
    """Create an API key and return the raw secret (shown once, never stored)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO api_key (key_hash, client_id, role, label, created_at) "
                 "VALUES (:h, :c, :r, :l, :t)"),
            {"h": hash_key(raw), "c": client_id, "r": role, "l": label, "t": _now()},
        )
    return raw


def authenticate(engine: Engine, raw_key: str | None) -> Principal | None:
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT client_id, role FROM api_key "
                 "WHERE key_hash = :h AND revoked = 0"),
            {"h": hash_key(raw_key)},
        ).fetchone()
    if row is None:
        return None
    return Principal(role=row.role, client_id=row.client_id)


def create_client(engine: Engine, *, name: str, sector: str, size_band: str,
                  region: str) -> tuple[str, str]:
    """Register a client organisation. Returns (client_id, raw_api_key)."""
    client_id = new_id("cli")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO client (client_id, name, sector, size_band, region, "
                 "created_at) VALUES (:i, :n, :s, :b, :r, :t)"),
            {"i": client_id, "n": name, "s": sector.strip().lower(),
             "b": size_band.strip().lower(), "r": region.strip().lower(), "t": _now()},
        )
    raw_key = issue_key(engine, client_id=client_id, role="client",
                        label=f"primary key for {name}")
    return client_id, raw_key
