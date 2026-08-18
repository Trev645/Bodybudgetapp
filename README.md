# AWA Workplace Utilisation Analytics & Benchmarking Platform

Turns raw workplace utilisation observation studies into defensible
analysis, design decisions and — at scale — original research. Built to
the AWA platform specification (draft v0.1) in three tiers: single
building → multi-client benchmarking → research engine.

## What it does

- **Ingestion** — clients upload their own data one building at a time
  (CSV or Excel): building map, teams, headcount, space schedule, then
  per-study observation rounds and the experience survey. A cleaning and
  validation step handles merged cells, inconsistent labels and missed
  rounds, and flags every problem before data is trusted downstream.
- **Analysis suite** (per study)
  - Core utilisation metrics: peak occupancy, average utilisation,
    frequency vs occupancy and the claimed-but-empty gap. Meeting rooms
    are a distinct module (no-show proxy, size-vs-need mismatch).
  - Desk-not-found risk: measured failure rate per team.
  - **Percentile desk sizing** — the flagship calculation. The acceptable
    failure rate is the input; the required desk count at that percentile
    of observed demand is the output (5% → 95th percentile).
  - The headcount cascade: assigned population × attendance ×
    utilisation → desk demand, plus the sharing ratio.
  - Experience overlay: perception scores against measured utilisation,
    with piecewise tipping-point detection.
- **Benchmarking** — compare any of your buildings against anonymised
  peers by sector, size band, region or any combination. Peer output is
  percentiles only, and a comparison is refused unless the peer group
  spans at least 3 distinct client organisations (k-anonymity).
- **Research engine** (admin only) — interrogates the pooled corpus for
  the relationship between assigned population, relative capacity
  (assigned ÷ desks) and perceived experience, including the universal
  tipping point and sector-by-sector flex.

## Security model

- API keys are stored as SHA-256 hashes only; keys are shown once at
  creation. `X-API-Key` header authenticates every request.
- Strict tenancy: every table carries `client_id` lineage; every query is
  scoped to the authenticated client. Cross-tenant probes return 404 so
  resource existence is never leaked.
- Anonymisation is enforced at query time (a view over lineage, not a
  copy): benchmark payloads contain aggregates only, never peer
  identities, and thin peer groups are refused outright.
- Admin role (client registration, corpus research) is a separate key
  class; client keys get 403 on admin routes.
- Uploads are size-capped and parsed defensively; headcount uploads that
  look like emails are flagged — the platform never needs identifiable
  people, only pseudonymous references.

## Quick start

```bash
pip install -e ".[dev]"          # or: pip install -r requirements.txt

python -m awa.cli init-db          # SQLite by default (awa.db)
python -m awa.cli create-admin-key # issue the bootstrap admin key
python -m awa.cli serve            # API on http://127.0.0.1:8000/docs

# Or explore immediately with a synthetic 6-client corpus:
python -m awa.cli demo
python -m awa.cli report <study_id> --failure-rate 5
```

For the multi-client deployment point `AWA_DATABASE_URL` at Postgres
(e.g. `postgresql+psycopg://user:pass@host/awa`); all SQL is portable.

## Client workflow (one building at a time)

```text
POST /buildings                          {name, gross_area_m2, region}
POST /buildings/{id}/settings            building map CSV/XLSX
POST /buildings/{id}/teams               team, allocated_desks
POST /buildings/{id}/headcount           person_ref, team, employment_type
POST /buildings/{id}/space-schedule      function, area_m2
POST /buildings/{id}/studies             {start_date, end_date, interval_mins}
POST /studies/{id}/observations          setting_code, ts, round, status, team
POST /studies/{id}/survey                response_id, team, item, score

GET  /studies/{id}/report?failure_rate=5   full analysis suite
GET  /studies/{id}/sizing?failure_rate=1   sizing at a different risk appetite
GET  /studies/{id}/issues                  ingestion validation flags
GET  /portfolio                            latest KPIs across your buildings
GET  /buildings/{id}/benchmark?dimensions=sector,size_band,region
```

Every ingestion response includes a validation report (`ok`, row counts,
and issues by severity) — nothing fails silently.

Observation `status` values: `occupied` (person present), `claimed`
(signs of use, nobody there), `empty`. Common client spellings
("signs of life", "vacant", Y/N, 1/0…) and column-name variants are
normalised automatically at ingestion.

## Layout

```
migrations/           SQL schema (multi-tenant from day one)
src/awa/
  db.py               engine + migration runner (SQLite / Postgres)
  security.py         API keys, tenancy principals, size bands
  ingestion/          cleaning + loaders (the front door)
  analysis/           metrics, sizing, cascade, experience
  benchmarking/       KPIs, anonymised peers, research engine
  api/                FastAPI service (auth, uploads, analyses)
  synthetic.py        demo corpus generator with known ground truth
  cli.py              init-db / create-admin-key / demo / report / serve
.claude/agents/       the utilisation-analyst sub-agent
tests/                hand-checked metrics, tenancy isolation, corpus e2e
```

## Notes on scope

- Space areas come from the structured space schedule. CAD (DWG) is a
  possible later enhancement; measurements are never inferred from PDF
  layouts (spec 3.3) — a quietly wrong benchmark is worse than none.
- Booked-vs-used for meeting rooms needs a booking-system feed; until
  then the claimed-but-empty rate serves as the no-show proxy.
- Cognitive-performance linkage: add cognitive-load items to the survey
  battery and they flow through the overlay and research queries
  automatically.
