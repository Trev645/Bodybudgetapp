"""Tenant isolation and anonymisation guarantees, tested through the API."""

import io

import pytest
from fastapi.testclient import TestClient

from awa.api.main import app
from awa.security import issue_key


@pytest.fixture()
def client(engine):
    with TestClient(app) as tc:
        yield tc


@pytest.fixture()
def admin_key(engine):
    return issue_key(engine, client_id=None, role="admin", label="test admin")


def register(client, admin_key, name, sector="finance", region="london"):
    r = client.post("/admin/clients",
                    json={"name": name, "sector": sector, "region": region},
                    headers={"X-API-Key": admin_key})
    assert r.status_code == 201
    return r.json()


def csv_upload(text):
    return {"file": ("data.csv", io.BytesIO(text.encode()), "text/csv")}


def test_requests_without_key_are_rejected(client):
    assert client.get("/buildings").status_code == 401
    assert client.post("/buildings", json={"name": "x", "region": "y"}
                       ).status_code == 401


def test_client_keys_cannot_use_admin_endpoints(client, admin_key):
    tenant = register(client, admin_key, "A Corp")
    r = client.post("/admin/clients",
                    json={"name": "evil", "sector": "x", "region": "y"},
                    headers={"X-API-Key": tenant["api_key"]})
    assert r.status_code == 403
    r = client.get("/admin/research/capacity-experience",
                   headers={"X-API-Key": tenant["api_key"]})
    assert r.status_code == 403


def test_cross_tenant_access_is_invisible(client, admin_key):
    a = register(client, admin_key, "A Corp")
    b = register(client, admin_key, "B Corp")
    ha, hb = {"X-API-Key": a["api_key"]}, {"X-API-Key": b["api_key"]}

    r = client.post("/buildings", json={"name": "A HQ", "region": "london",
                                        "gross_area_m2": 12000}, headers=ha)
    building_id = r.json()["building_id"]

    # B sees an empty portfolio and gets 404 (not 403) probing A's building.
    assert client.get("/buildings", headers=hb).json() == []
    assert client.post(f"/buildings/{building_id}/studies",
                       json={"start_date": "2026-03-02",
                             "end_date": "2026-03-06"},
                       headers=hb).status_code == 404
    assert client.post(f"/buildings/{building_id}/settings",
                       files=csv_upload("setting_code,type\nD1,desk"),
                       headers=hb).status_code == 404
    # A can use it normally.
    assert client.post(f"/buildings/{building_id}/settings",
                       files=csv_upload("setting_code,type\nD1,desk"),
                       headers=ha).status_code == 200


def test_upload_analyse_roundtrip(client, admin_key):
    t = register(client, admin_key, "Roundtrip Ltd")
    h = {"X-API-Key": t["api_key"]}
    building_id = client.post(
        "/buildings", json={"name": "HQ", "region": "london",
                            "gross_area_m2": 9000},
        headers=h).json()["building_id"]
    client.post(f"/buildings/{building_id}/settings", headers=h, files=csv_upload(
        "setting_code,type,floor,team\nD1,desk,1,Ops\nD2,desk,1,Ops"))
    client.post(f"/buildings/{building_id}/headcount", headers=h, files=csv_upload(
        "person_ref,team,employment_type\nP1,Ops,employee\nP2,Ops,employee\n"
        "P3,Ops,contractor"))
    study_id = client.post(
        f"/buildings/{building_id}/studies",
        json={"start_date": "2026-03-02", "end_date": "2026-03-03"},
        headers=h).json()["study_id"]
    r = client.post(f"/studies/{study_id}/observations", headers=h,
                    files=csv_upload(
        "setting_code,ts,round,status,team\n"
        "D1,2026-03-02T10:00,R1,occupied,Ops\n"
        "D2,2026-03-02T10:00,R1,empty,\n"
        "D1,2026-03-02T14:00,R2,occupied,Ops\n"
        "D2,2026-03-02T14:00,R2,occupied,Ops\n"))
    assert r.json()["ok"], r.json()
    report = client.get(f"/studies/{study_id}/report?failure_rate=5",
                        headers=h).json()
    assert report["desks"]["peak_occupancy_pct"] == 100.0
    assert report["desks"]["average_utilisation_pct"] == 75.0
    assert report["headcount_cascade"]["assigned_population"] == 3
    assert report["desk_sizing"]["building"]["required_desks_pooled"] == 2


def test_benchmark_refuses_thin_peer_groups(client, admin_key, engine):
    from awa.synthetic import seed_demo
    # Two other clients only — below the k-anonymity threshold of 3.
    seed_demo(engine, n_clients=2, days=2, rounds_per_day=1)
    me = register(client, admin_key, "Small Pool Ltd", sector="finance",
                  region="london")
    h = {"X-API-Key": me["api_key"]}
    building_id = client.post(
        "/buildings", json={"name": "HQ", "region": "london",
                            "gross_area_m2": 9000},
        headers=h).json()["building_id"]
    r = client.get(f"/buildings/{building_id}/benchmark?dimensions=region",
                   headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "at least 3" in body["reason"]


def test_console_served_and_studies_listed(client, admin_key):
    page = client.get("/")
    assert page.status_code == 200
    assert "AWA Utilisation Console" in page.text
    t = register(client, admin_key, "Console Ltd")
    h = {"X-API-Key": t["api_key"]}
    building_id = client.post(
        "/buildings", json={"name": "HQ", "region": "london"},
        headers=h).json()["building_id"]
    sid = client.post(f"/buildings/{building_id}/studies",
                      json={"start_date": "2026-03-02",
                            "end_date": "2026-03-06"},
                      headers=h).json()["study_id"]
    listed = client.get(f"/buildings/{building_id}/studies", headers=h).json()
    assert [s["study_id"] for s in listed] == [sid]
    # Other tenants get 404 on the same listing.
    other = register(client, admin_key, "Other Ltd")
    assert client.get(f"/buildings/{building_id}/studies",
                      headers={"X-API-Key": other["api_key"]}).status_code == 404


def test_admin_manages_organisations_and_upload_control(client, admin_key):
    ha = {"X-API-Key": admin_key}
    t = register(client, admin_key, "Managed Ltd")
    h = {"X-API-Key": t["api_key"]}

    assert client.get("/me", headers=ha).json()["role"] == "admin"
    me = client.get("/me", headers=h).json()
    assert me["role"] == "client" and me["uploads_enabled"] is True

    orgs = client.get("/admin/clients", headers=ha).json()
    org = next(o for o in orgs if o["client_id"] == t["client_id"])
    assert org["uploads_enabled"] is True and org["active_keys"] == 1

    building_id = client.post(
        "/buildings", json={"name": "HQ", "region": "london"},
        headers=h).json()["building_id"]

    # Switch uploads off: data entry blocked with a clear message,
    # viewing still allowed.
    r = client.patch(f"/admin/clients/{t['client_id']}",
                     json={"uploads_enabled": False}, headers=ha)
    assert r.json()["uploads_enabled"] is False
    blocked = client.post(f"/buildings/{building_id}/settings",
                          files=csv_upload("setting_code,type\nD1,desk"),
                          headers=h)
    assert blocked.status_code == 403
    assert "disabled" in blocked.json()["detail"]
    assert client.get("/portfolio", headers=h).status_code == 200

    # Switch back on: uploads work again.
    client.patch(f"/admin/clients/{t['client_id']}",
                 json={"uploads_enabled": True}, headers=ha)
    assert client.post(f"/buildings/{building_id}/settings",
                       files=csv_upload("setting_code,type\nD1,desk"),
                       headers=h).status_code == 200

    # Clients cannot use the management endpoints.
    assert client.get("/admin/clients", headers=h).status_code == 403
    assert client.patch(f"/admin/clients/{t['client_id']}",
                        json={"uploads_enabled": False},
                        headers=h).status_code == 403


def test_admin_key_reissue_revokes_old_key(client, admin_key):
    ha = {"X-API-Key": admin_key}
    t = register(client, admin_key, "Rotate Ltd")
    old = {"X-API-Key": t["api_key"]}
    assert client.get("/buildings", headers=old).status_code == 200

    r = client.post(f"/admin/clients/{t['client_id']}/reissue-key", headers=ha)
    new_key = r.json()["api_key"]
    assert new_key != t["api_key"]
    assert client.get("/buildings", headers=old).status_code == 401
    assert client.get("/buildings",
                      headers={"X-API-Key": new_key}).status_code == 200


def test_benchmark_bad_dimensions_rejected(client, admin_key):
    me = register(client, admin_key, "Dims Ltd")
    h = {"X-API-Key": me["api_key"]}
    building_id = client.post(
        "/buildings", json={"name": "HQ", "region": "london"},
        headers=h).json()["building_id"]
    r = client.get(f"/buildings/{building_id}/benchmark?dimensions=client_name",
                   headers=h)
    assert r.status_code == 422
