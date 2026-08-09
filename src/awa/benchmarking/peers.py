"""Anonymised peer benchmarking (spec Section 5).

A client compares a building against buildings of similar sector, size
band and/or region — any combination — drawn from OTHER clients' studies.

Anonymisation is enforced at query time, by design:
- peers are aggregated to percentiles; no per-building rows leave the pool;
- a comparison is refused unless the peer group spans at least
  MIN_PEER_CLIENTS distinct client organisations (k-anonymity), so a thin
  peer group can never be reverse-engineered to a single competitor.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from awa.benchmarking.kpis import kpi_percentiles, latest_study_id, study_kpis

MIN_PEER_CLIENTS = 3
VALID_DIMENSIONS = {"sector", "size_band", "region"}


def benchmark_building(engine: Engine, client_id: str, building_id: str,
                       dimensions: list[str]) -> dict:
    dims = [d.strip().lower() for d in dimensions if d.strip()]
    bad = set(dims) - VALID_DIMENSIONS
    if bad or not dims:
        raise ValueError(
            f"dimensions must be a non-empty subset of {sorted(VALID_DIMENSIONS)}")

    with engine.connect() as conn:
        subject = conn.execute(text(
            "SELECT b.building_id, b.name, b.region, b.size_band, c.sector "
            "FROM building b JOIN client c ON c.client_id = b.client_id "
            "WHERE b.building_id = :b AND b.client_id = :c"),
            {"b": building_id, "c": client_id}).fetchone()
    if subject is None:
        raise LookupError("building not found for this client")

    match = {"sector": subject.sector, "size_band": subject.size_band,
             "region": subject.region}
    where = " AND ".join(
        {"sector": "c.sector = :sector",
         "size_band": "b.size_band = :size_band",
         "region": "b.region = :region"}[d] for d in dims)
    params = {d: match[d] for d in dims}
    params["client_id"] = client_id
    with engine.connect() as conn:
        peers = conn.execute(text(
            f"SELECT b.building_id, b.client_id FROM building b "
            f"JOIN client c ON c.client_id = b.client_id "
            f"WHERE b.client_id != :client_id AND {where}"),
            params).fetchall()

    peer_rows, peer_clients = [], set()
    for p in peers:
        sid = latest_study_id(engine, p.building_id)
        if sid is None:
            continue
        kpis = study_kpis(engine, sid)
        if kpis:
            peer_rows.append(kpis)
            peer_clients.add(p.client_id)

    result = {
        "building": subject.name,
        "dimensions": dims,
        "peer_criteria": {d: match[d] for d in dims},
    }
    if len(peer_clients) < MIN_PEER_CLIENTS:
        result["available"] = False
        result["reason"] = (
            f"peer group has {len(peer_clients)} other client(s) with studies; "
            f"at least {MIN_PEER_CLIENTS} are required before anonymised "
            f"comparison is allowed — try broader dimensions")
        return result

    own_sid = latest_study_id(engine, building_id)
    own = study_kpis(engine, own_sid) if own_sid else None
    peer_stats = kpi_percentiles(peer_rows)
    comparison = {}
    if own:
        for field, stats in peer_stats.items():
            value = own.get(field)
            if value is None:
                continue
            position = ("below peer median" if value < stats["median"]
                        else "above peer median" if value > stats["median"]
                        else "at peer median")
            comparison[field] = {"you": value, **stats, "position": position}
    result.update({
        "available": True,
        "peer_buildings": len(peer_rows),
        "peer_clients": len(peer_clients),
        "your_kpis": own,
        "peer_percentiles": peer_stats,
        "comparison": comparison,
    })
    return result
